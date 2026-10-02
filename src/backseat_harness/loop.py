"""The core loop: plan -> (spawn -> wait -> review)* -> terminal state.

``run_task`` is the library entry point a future orchestrator imports. The CLI
(cli.py) is a thin wrapper over it. The loop is the only writer to the task's
``.backseat`` files; every persisted mutation (checklist merge, binding update,
state, ledger) happens here after the operator's verdict is parsed.

When a run starts with no prompt for the managed agent -- a brand-new task, or a
resumed one -- the operator goes first in a **plan** phase: it plans (or
re-plans) the checklist and writes the first prompt, exactly as a human operator
would before touching the keyboard. Every managed turn after that is followed
by a **review**.

The managed-agent spawn and operator invocation are injected so this module
has no hard dependency on a live CLI. cli.py wires the real runners; tests wire
fakes.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from backseat_harness import capture as capture_mod
from backseat_harness import checklist as checklist_mod
from backseat_harness import compact as compact_mod
from backseat_harness import history, progress, storage
from backseat_harness import marker as marker_mod
from backseat_harness import operator as op
from backseat_harness.config import Config, TaskPaths
from backseat_harness.models import (
    Binding,
    LedgerEntry,
    TaskResult,
    TaskState,
    TaskStatus,
    TurnOutcome,
    Verdict,
    VerdictKind,
    next_generation,
)
from backseat_harness.providers import Provider
from backseat_harness.providers.base import SpawnSpec, TurnResult

# Runs one managed turn and returns its outcome. Injected (real subprocess/fake).
ManagedRunner = Callable[[Provider, SpawnSpec], TurnResult]
# Invokes the operator with an assembled prompt and returns its final text,
# an OperatorReply when the call was killed (any other str reply was not).
OperatorInvoker = Callable[[str], str]


class OperatorReply(str):
    """An operator reply that remembers whether its call was killed.

    A ``str`` subclass so an invoker that merely returns text (tests, other
    frontends) is still a valid reply: an unflagged reply was not timed out.
    The flag originates where the kill is actually known -- the cli layer
    reading ``spawn.run_with_timeout``'s ``timed_out`` -- so the loop's re-ask
    conditions the timeout story on the kill rather than on the text being
    empty: an empty reply from a completed call was never killed.
    """

    def __new__(cls, text: str, *, timed_out: bool = False) -> OperatorReply:
        reply = super().__new__(cls, text)
        reply.timed_out = timed_out
        return reply


def _slot_complaint(bad: list[tuple[str, str]], config: Config) -> str:
    """The correction text for a verdict naming slots no ``[models]`` defines.

    Names the offending step ids and values, the configured slots, and the two
    things that are always acceptable -- the reserved ``default`` name and
    omitting the field -- so the operator can fix the response without having to
    recall the rules from an earlier turn.
    """
    bad_text = ", ".join(f"{value!r} on step {sid!r}" for sid, value in bad)
    which = "which is" if len(bad) == 1 else "none of which is"
    slots = ", ".join(sorted(config.models))
    valid_line = (
        f'Valid slots: {slots} (or "default"; omit for the default model).'
        if slots
        else 'No model slots are configured; do not set "model".'
    )
    return (
        f'Your checklist_update set "model" to {bad_text}, {which} not a '
        f"configured slot. {valid_line} Re-send it with a valid \"model\" (or none)."
    )


def _model_rejection_warning(
    config: Config,
    provider: Provider,
    model_id: str,
    turn_model: str | None,
    slot_source: tuple[str, str] | None,
) -> str:
    """The loud complaint for a turn the provider refused to run.

    ``model_id`` is the id the CLI itself quoted back (kiro names it in its
    ``runError``), and the origin says who asked for it, so the operator can
    go straight to the binding that produced it: the step's slot when a slot
    supplied the id, otherwise the default binding's own name. A rejection
    with no ``--model`` composed at all cannot be the harness's doing, and
    says so rather than blaming a binding that was never used.

    The remedy hint rides with the complaint when the adapter knows the
    command that lists valid ids, so "which ids are valid?" is answered in
    the same breath as the failure.
    """
    subject = f"{config.provider} rejected model id {model_id!r}"
    if slot_source is not None:
        sid, slot = slot_source
        origin = f"step '{sid}' slot '{slot}'"
    elif turn_model is not None:
        bound = config.models.get("default")
        origin = "[models] default" if isinstance(bound, str) and bound else "[harness] model"
    else:
        return (
            f"{subject} with no --model composed for this turn; the turn "
            "produced no reply (the id came from the provider's own selection)"
        )
    hint = (
        f" Fix the model id -- `{provider.list_models_command}` lists the ids "
        f"{config.provider} accepts"
        if provider.list_models_command
        else ""
    )
    return f"{subject} ({origin}); the turn produced no reply.{hint}"


def run_task(
    slug: str,
    config: Config,
    provider: Provider,
    managed_runner: ManagedRunner,
    operator_invoker: OperatorInvoker,
    first_prompt: str | None = None,
) -> TaskResult:
    """Drive a single task lane to a terminal state.

    ``first_prompt`` is an optional override for the first managed turn. Without
    it, the operator plans first and writes that prompt itself.
    """
    paths = config.task_paths(slug)
    state = storage.load_state(paths, slug)
    state.status = TaskStatus.CONTINUE
    state.reason = ""
    binding = _refresh_size(
        storage.load_binding(paths) or _new_binding(config.provider), provider
    )
    meta = storage.read_meta(paths)
    task_dir = paths.root.relative_to(config.workspace_root).as_posix()
    paths.docs.mkdir(parents=True, exist_ok=True)
    # The managed turn's flags are composed per spawn below (docs 2-plan §3):
    # extra_args and --agent are fixed, but --model is resolved from the active
    # step's slot on every turn, so nothing --model-related is baked in here.

    # §5a: a stripped bad slot rides the slot_warning channel of the NEXT
    # operator prompt — carried here across turns, never persisted to disk.
    slot_residue: str | None = None

    def call_operator(
        outcome: TurnOutcome | None,
        transcript_file: str | None,
        slot_warning: str | None = None,
    ) -> Verdict:
        nonlocal slot_residue
        inputs = op.ReviewInputs(
            meta_prompt=meta,
            checklist=checklist_mod.load_checklist(paths.checklist),
            managed_fork=_operator_fork(binding, provider),
            outcome=outcome,
            iteration=state.iteration,
            task_dir=task_dir,
            verify_commands=list(config.operator.verify_commands),
            model_slots=tuple(sorted(config.models)),
            slot_warning=slot_warning,
            docs=_list_docs(paths, config.workspace_root),
            transcript_file=transcript_file,
        )
        # One budget for every unusable reply, whatever made it unusable. An
        # unparsable envelope and a bad model slot are the same failure to the
        # loop -- the operator's answer was not usable -- so they draw on the
        # same allowance, and each attempt names every complaint so far so a
        # re-ask corrects rather than repeats. Each attempt yields at most one
        # complaint (a verdict that did not parse has no checklist_update to
        # check), so the suffix stays one or two sentences.
        #
        # The re-ask re-uses the *same assembled prompt*: rebuilding from the
        # identical frozen inputs gives the identical text, and a checklist that
        # would persist on a strike stays unpersisted until the verdict is
        # accepted. Slot validation lives here, never in parse_verdict, because
        # this is the layer that owns config.models (§5a).
        budget = max(1, config.operator_retry_budget)
        complaints: list[tuple[str, str]] = []
        attempts = 0
        verdict: Verdict | None = None
        bad: list[tuple[str, str]] = []
        while True:
            attempts += 1
            prompt = op.build_prompt(inputs)
            if complaints:
                prompt += "\n\n" + " ".join(text for _, text in complaints) + (
                    " Re-send the complete JSON response."
                    " Output the JSON object only, no prose."
                )
            text = operator_invoker(prompt)
            verdict = op.parse_verdict(text)
            if verdict is None:
                # Three faults, three corrections. An empty reply licenses the
                # timeout story only when the call actually timed out: the flag
                # rides the reply from the layer that killed it, while an empty
                # reply from a completed call was never killed -- claiming a
                # kill sends the operator to shorten a budget that never bound.
                # An unreadable reply is the envelope never being found, no
                # matter what made it non-empty.
                if text.strip():
                    complaints.append(
                        (
                            "unparsable",
                            "Your previous response contained no parsable verdict JSON.",
                        )
                    )
                elif getattr(text, "timed_out", False):
                    complaints.append(
                        (
                            "empty",
                            f"Your previous response was empty: the operator call hit "
                            f"its {config.operator.timeout_s}s timeout and its process "
                            "tree was terminated. Answer again, keeping any validation "
                            "tool calls short enough to finish inside that budget.",
                        )
                    )
                else:
                    complaints.append(
                        (
                            "empty",
                            "Your previous response was empty: the operator call ended "
                            "without timing out, so nothing was killed, but no text "
                            "came back. Answer again, sending the complete JSON "
                            "response.",
                        )
                    )
            else:
                bad = _bad_model_slots(verdict, config)
                if not bad:
                    break
                complaints.append(("a bad model slot", _slot_complaint(bad, config)))
            if attempts >= budget:
                break
            print(
                f"[warn] task: operator attempt {attempts}/{budget} was "
                f"{complaints[-1][0]}; retrying",
                file=sys.stderr,
            )

        residue: str | None = None
        if verdict is None:
            # Every attempt failed to yield a verdict. Say so in the ledger
            # notes (_persist) and the operator-note line a watcher reads
            # (_report) -- plus the finish reason if this verdict lands
            # terminal -- rather than leaving a bare 'problem' with no cause.
            # The notes reach no checklist and no later prompt; only those
            # sinks carry them.
            reasons = ", ".join(label for label, _ in complaints)
            verdict = Verdict(
                kind=VerdictKind.PROBLEM,
                notes=(
                    f"operator produced no parsable verdict on {attempts} attempt(s) "
                    f"({reasons})"
                ),
            )
        elif bad:
            verdict, dropped = _strip_invalid_model_slots(verdict, config)
            residue = (
                "operator verdict still named unknown model slot(s) after "
                f"{attempts} attempt(s), dropping: "
                + "; ".join(f"{value!r} on step {sid!r}" for sid, value in dropped)
            )
            print(f"[warn] task: {residue}", file=sys.stderr)
        slot_residue = residue
        return verdict

    started = time.monotonic()
    pending_prompt = first_prompt
    pending_reset = False
    last_verdict: VerdictKind | None = None
    # Consecutive PROBLEM verdicts that re-sent the prompt just sent (docs 2-plan
    # §6). Reset by any continue, and by a PROBLEM that changes the prompt.
    problem_repeats = 0

    # --- Plan phase: no prompt queued, so the operator drives first. ---
    if pending_prompt is None:
        progress.iteration_start(state.iteration, state.generation, "plan")
        # Capture before the call, exactly as after a turn: on a resume the file
        # on disk predates whatever advanced the session since (a takeover, a
        # crash), and re-planning wants the same untruncated record reviewing
        # does. On a fresh task the lookup finds nothing -- this root UUID has
        # never run -- so no file is written and the prompt names none.
        prior = _capture_transcript(paths, binding, provider, config.workspace_root)
        # §5b, plan phase: this block runs no managed turn, so the per-turn
        # resolution below has not happened yet -- but the plan phase is the
        # default resume path (first_prompt defaults to None), and the operator
        # it calls is the one whose job is to correct the checklist, so an
        # on-disk bad slot has to reach this prompt too, on both channels.
        # Same fresh disk read the turn loop uses; the model is discarded
        # (nothing is spawned here) and the review phase re-resolves, so the
        # warning is one-shot rather than carried across the boundary. The kiro
        # advisory below is deliberately not applied here: it is a standing
        # caveat about kiro's missing serving-model field, it quotes the id a
        # slot resolved, and this phase composes no flag and runs no turn.
        _plan_model, _plan_slot_source, plan_warning = _resolve_turn_model(
            checklist_mod.load_checklist(paths.checklist), config
        )
        verdict = call_operator(None, prior, slot_warning=plan_warning)
        _persist(paths, state, binding, verdict, None, phase="plan")
        _report(verdict)
        last_verdict = verdict.kind
        terminal = _terminal(paths, state, verdict)
        if terminal is not None:
            return terminal
        pending_prompt = verdict.next_prompt
        pending_reset = verdict.reset_context

    while True:
        # --- Budget guards (cheap, forward-only). ---
        if state.iteration >= config.max_iterations:
            return _finish(paths, state, TaskStatus.BLOCKED, last_verdict, "max_iterations reached")
        if time.monotonic() - started >= config.max_wall_clock_s:
            return _finish(paths, state, TaskStatus.BLOCKED, last_verdict, "max_wall_clock reached")
        if pending_prompt is None:
            return _finish(
                paths, state, TaskStatus.BLOCKED, last_verdict, "no prompt to send"
            )

        state.iteration += 1
        progress.iteration_start(state.iteration, state.generation, "review")

        # --- Context decision (before spawn): resume, compact, or reset. ---
        gen = binding.current
        transcript_bytes = gen.transcript_bytes if gen else 0
        resume_id = gen.native_id if gen else None
        prompt = pending_prompt

        if pending_reset:
            # Operator abandoned the session chat: fresh session, no fork, no
            # resume -- only the marker and the next prompt.
            new_n = (gen.n + 1) if gen else 1
            binding = next_generation(binding, new_n)
            state.generation = new_n
            resume_id = None
            prompt = marker_mod.prepend_marker(binding.root_uuid, new_n, pending_prompt)
            pending_reset = False
        elif gen and compact_mod.should_compact(transcript_bytes, config):
            if compact_mod.can_compact_again(gen.n, config):
                fork_text = compact_mod.render_fork(binding.root_uuid, provider.history_provider)
                # New generation: fresh session, marker with incremented #n.
                binding = next_generation(binding, gen.n + 1)
                state.generation = gen.n + 1
                resume_id = None
                messages = compact_mod.build_compacted_prompt(fork_text, pending_prompt)
                prompt = "\n\n".join(messages)
                prompt = marker_mod.prepend_marker(binding.root_uuid, state.generation, prompt)
            # else: over budget on generations; proceed without compacting.
        elif gen is None:
            # First ever turn: apply the marker to establish identity.
            binding = next_generation(binding, 1)
            state.generation = 1
            prompt = marker_mod.prepend_marker(binding.root_uuid, 1, pending_prompt)

        # --- Model resolution (per turn, from the current checklist). ---
        turn_model, slot_source, slot_warning = _resolve_turn_model(
            checklist_mod.load_checklist(paths.checklist), config
        )
        if slot_source is not None and config.provider == "kiro":
            kiro_sid, kiro_slot = slot_source
            # Composed, not overwritten: a slot-sourced turn can also carry a
            # warning of its own (a disagreement, an unusable [models].default),
            # and both reach the prompt together.
            #
            # What this advisory says changed with the CLI. On 2.26.1 kiro
            # applies --model on fresh and resumed turns (docs 1, "Re-test on
            # kiro-cli 2.26.1"), so the line this used to print -- "provider
            # kiro does not honor --model ... will run on kiro's default
            # routing anyway" -- stated as fact something measured false on
            # the installed version, on every slot-sourced kiro turn. The one
            # thing kiro still does not expose is any field saying which model
            # *served* the turn: the ACP stream carries no model, and the
            # sidecar records the request (`user_turn_metadatas[].model`), not
            # an attestation. That residual, and nothing else, is claimed
            # here -- on stderr and in the operator prompt alike. The failure
            # that is real (an id kiro does not have) is caught after the
            # spawn, where kiro quotes the id back.
            kiro_warning = (
                f"provider kiro exposes no per-turn serving-model field; step "
                f"'{kiro_sid}' slot '{kiro_slot}' requests {turn_model!r} and "
                f"kiro's own records carry only the request"
            )
            print(f"[warn] {kiro_warning}", file=sys.stderr)
            slot_warning = (
                f"{kiro_warning}; {slot_warning}" if slot_warning else kiro_warning
            )

        # --- Spawn + wait (turn end == process exit). ---
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=resume_id,
            timeout_s=config.turn_timeout_s,
            # Order is a documented contract (docs 2-plan E8): the owner's
            # extra_args first, then the turn's --model (its own extra_args
            # --model is the baseline the slot appends after), then --agent.
            extra_args=(
                config.extra_args
                + (("--model", turn_model) if turn_model else ())
                + (("--agent", config.agent) if config.agent else ())
            ),
        )
        result = managed_runner(provider, spec)
        # The refusal that can be named exactly, caught where it happens. kiro
        # quotes the id back in its runError, so the binding that asked for it
        # can be quoted to the operator instead of the turn just ending as an
        # unnamed nonzero exit; fresh and resumed turns emit the same event, so
        # one check covers both paths (docs/12-kiro-loud-path-live.md). Warnings
        # collected before the spawn stay in ``slot_warning`` too: the review
        # prompt carries the advisory and this complaint on one line.
        rejected = provider.rejected_model_id(result.stdout, result.stderr)
        if rejected is not None:
            rejection = _model_rejection_warning(
                config, provider, rejected, turn_model, slot_source
            )
            print(f"[warn] {rejection}", file=sys.stderr)
            slot_warning = (
                f"{slot_warning}; {rejection}" if slot_warning else rejection
            )
        binding = _record_native(binding, result, provider)
        outcome = TurnOutcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_s=result.duration_s,
            native_session_id=result.native_session_id,
            transcript_bytes=binding.current.transcript_bytes if binding.current else 0,
        )

        # --- Capture the full session transcript for review. ---
        transcript_file = _capture_transcript(paths, binding, provider, config.workspace_root)

        # --- Review: a fresh operator assesses the turn. ---
        # Any verdict-slot residue from the previous call rides this prompt
        # (one-shot: the delta it describes no longer exists after this).
        review_warning = slot_warning
        if slot_residue:
            review_warning = (
                f"{slot_residue}; {slot_warning}" if slot_warning else slot_residue
            )
        verdict = call_operator(outcome, transcript_file, slot_warning=review_warning)
        _persist(paths, state, binding, verdict, result, phase="review")
        _report(verdict)
        last_verdict = verdict.kind

        # --- Decide next action. ---
        terminal = _terminal(paths, state, verdict)
        if terminal is not None:
            return terminal
        # continue or problem: repeat with the next prompt (problem repeats
        # naturally). The fall-through above is unchanged and stays a parse-level
        # rule; the cap below bounds the loop, not the parse.
        effective_prompt = verdict.next_prompt or pending_prompt
        # A PROBLEM that re-sends the prompt just sent is the stuck shape: the
        # turn failed, the operator had nothing new to say, so the next turn is
        # byte-identical. The null-next_prompt fall-through lands here too, by
        # construction -- and so do the synthesized PROBLEMs for an unparseable
        # verdict, which is the loop burning turns on one prompt. Any change to
        # the effective prompt (or a continue) clears the count, so a genuinely
        # iterating recovery is still bounded only by max_iterations.
        if verdict.kind is VerdictKind.PROBLEM and effective_prompt == pending_prompt:
            problem_repeats += 1
            if 0 < config.problem_repeat_cap and problem_repeats >= config.problem_repeat_cap:
                return _finish(
                    paths, state, TaskStatus.BLOCKED, last_verdict,
                    f"problem stuck: same prompt resent {problem_repeats} turns in a row",
                )
        else:
            problem_repeats = 0
        pending_prompt = effective_prompt
        pending_reset = verdict.reset_context


def _default_binding(config: Config) -> tuple[str | None, str | None]:
    """The default model binding: ``[models].default`` when it binds a real id,
    else ``[harness].model``, else no flag at all (today's behavior, unchanged).

    Returns ``(model_id, warning)``. ``load_config`` already drops a ``default``
    whose value is not a non-empty string, so a warning here is only reachable
    from a ``Config`` built some other way — but reaching it composed *no*
    ``--model`` at all, on a run whose ``[harness].model`` was set, with nothing
    on any channel: the same never-silent shape the empty named slot has, and
    the one place the default path could still swallow a configured value. So
    the deviation is named here, once per turn, and rides back to the operator
    prompt beside the slot warnings.
    """
    bound = config.models.get("default")
    if isinstance(bound, str) and bound:
        return bound, None
    if bound is None:
        return config.model, None
    warning = (
        f"[models] default is {bound!r}, not a model id; using [harness].model "
        f"({config.model!r}) instead"
    )
    print(f"[warn] config: {warning}", file=sys.stderr)
    return config.model, warning


def _resolve_turn_model(
    checklist: checklist_mod.Checklist, config: Config
) -> tuple[str | None, tuple[str, str] | None, str | None]:
    """Resolve the concrete model for one managed turn (docs 2-plan §3/§5b/§7).

    The active step(s) drive the slot. Exactly one active step names the slot
    that resolves through ``[models]``; several active steps that disagree fall
    back to the first in insertion order, warned about; none active — or a step
    whose ``model`` is missing, null, or not a string — resolves to the default
    binding (see ``_default_binding``). Recomputed by the caller every turn from
    a fresh disk read, since the operator rewrites the checklist between turns,
    so a warning that describes an unresolved checklist is re-raised every turn
    until the operator fixes it.

    Returns ``(model_id, slot_source, slot_warning)``. ``slot_source`` is the
    ``(step_id, slot)`` pair when a step's named slot produced ``model_id``,
    and ``None`` for the default/config.model fallback — the kiro warning keys
    on that provenance, exactly when a slot (rather than the run-wide baseline)
    supplied the id. A step naming the literal ``default`` counts as the
    fallback (plan §1: naming it == omitting the field): no slot_source, no
    unknown-slot warning.

    ``slot_warning`` is the *joined* text of every cause that fired this turn —
    the disagreement, an on-disk bad or unusable slot, a non-string ``model``,
    an unusable ``[models].default``. Each prints to stderr here (never
    silent), and all of them surface in one ``Slot warning:`` line in the
    operator prompt, separated by ``"; "`` — the operator is the party that can
    correct any of them, so a cause that fires alongside another is not
    dropped. The two channels are the same ones §5b established for a bad slot,
    which is what plan §4 asked for the disagreement too.
    """
    warnings: list[str] = []
    active = [
        (sid, step)
        for sid, step in checklist.steps.items()
        if step.get("status") == "active"
    ]
    if not active:
        model, note = _default_binding(config)
        return model, None, note

    sid, step = active[0]

    if len(active) > 1 and len({step.get("model") for _, step in active}) > 1:
        detail = ", ".join(f"{s}: {s_step.get('model')!r}" for s, s_step in active)
        # Plan §4 asks for this "noted in the next operator prompt … so the
        # operator can fix it", and the operator is the only party who can: the
        # deterministic first-active pick is the harness's tie-break, not an
        # answer. Collected before the step's own checks so a turn that is both
        # ambiguous and bad-slot reports both rather than the last one wins.
        warning = (
            f"active steps disagree on a model slot ({detail}); using the first "
            "active step's slot for this turn"
        )
        print(f"[warn] task: {warning}", file=sys.stderr)
        warnings.append(warning)

    raw = step.get("model")
    if raw is not None and not isinstance(raw, str):
        warning = (
            f"step '{sid}' has a non-string model field {raw!r}; treated as "
            "unset (no slot)"
        )
        print(f"[warn] task: {warning}", file=sys.stderr)
        warnings.append(warning)
        raw = None

    if isinstance(raw, str) and raw != "default":
        # On-disk unknown slot: fall back loudly, never silently. A literal
        # "default" is not unknown — plan §1: naming it == omitting the field,
        # so it resolves through the same fallback with no slot_source (and
        # therefore no kiro warning either).
        bound = config.models.get(raw)
        if isinstance(bound, str) and bound:
            # A clean slot can still arrive on a warned turn (a disagreement
            # above), so the collected text rides back even though this path
            # raises nothing of its own.
            return bound, (sid, raw), "; ".join(warnings) or None
        if raw in config.models:
            # A slot that exists but binds nothing usable (an empty id). Parsing
            # already drops such pairs, so this needs a Config built some other
            # way — but the failure it causes is the silent one: returning it
            # would compose no --model (falsy at the spawn site) while
            # slot_source claimed a slot supplied the model, and the prompt
            # would still advertise the name. Same shape as unknown, named.
            warning = (
                f"step '{sid}' names model slot '{raw}', whose [models] value "
                f"is {bound!r}, not a model id; running the default model instead"
            )
        else:
            warning = (
                f"step '{sid}' names model slot '{raw}', which no [models] "
                "config defines; running the default model instead"
            )
        print(f"[warn] task: {warning}", file=sys.stderr)
        warnings.append(warning)
    model, note = _default_binding(config)
    if note:
        warnings.append(note)
    return model, None, "; ".join(warnings) or None


def _valid_model_slot(slot: Any, config: Config) -> bool:
    """A slot value is valid iff it names a ``[models]`` key (docs 2-plan §5a).

    Absent/None is fine (means the default binding), so is the literal
    ``"default"`` even when ``[models]`` does not define it: per plan §1 that
    name resolves exactly like naming no slot at all. Anything else — including
    an empty string or a non-string — is a bad slot.
    """
    return slot is None or (
        isinstance(slot, str) and (slot in config.models or slot == "default")
    )


def _bad_model_slots(verdict: Verdict, config: Config) -> list[tuple[str, str]]:
    """``(step_id, bad_value)`` pairs for verdict-set ``model`` fields (§5a).

    Only *present string values that are not valid slots* count: absent/None is
    "use the default model", not an error.
    """
    collect = []
    raw_steps = verdict.checklist_update.get("steps")
    if isinstance(raw_steps, dict):
        for sid, raw in raw_steps.items():
            if not isinstance(raw, dict):
                continue
            slot = raw.get("model")
            if slot is not None and not _valid_model_slot(slot, config):
                collect.append((sid, slot))
    return collect


def _strip_invalid_model_slots(
    verdict: Verdict, config: Config
) -> tuple[Verdict, list[tuple[str, str]]]:
    """Copy ``verdict`` without invalid ``model`` keys in checklist_update.steps.

    Merge-protocol hygiene before ``_persist``: the stripped value never reaches
    disk. Returns the sanitized verdict and ``(step_id, bad_value)`` pairs.
    """
    steps = verdict.checklist_update.get("steps")
    if not isinstance(steps, dict):
        return verdict, []
    removed: list[tuple[str, str]] = []

    def clean(step_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        slot = raw.get("model")
        if slot is not None and not _valid_model_slot(slot, config):
            removed.append((step_id, slot))
            return {k: v for k, v in raw.items() if k != "model"}
        return raw

    cleaned_steps: dict[str, Any] = {
        sid: clean(sid, raw) if isinstance(raw, dict) else raw
        for sid, raw in steps.items()
    }
    verdict = replace(
        verdict,
        checklist_update={**verdict.checklist_update, "steps": cleaned_steps},
    )
    return verdict, removed


def _list_docs(paths: TaskPaths, workspace_root) -> list[str]:
    """Workspace-relative paths of every file under the task's docs dir, sorted."""
    if not paths.docs.is_dir():
        return []
    return sorted(
        p.relative_to(workspace_root).as_posix()
        for p in paths.docs.rglob("*")
        if p.is_file()
    )


def _capture_transcript(
    paths: TaskPaths,
    binding: Binding,
    provider: Provider,
    workspace_root,
) -> str | None:
    """Overwrite ``transcript.md`` with the session so far; return its rel path.

    Best-effort observability: capture reads the managed session through
    chat-mother-forker, which can be unavailable or empty (e.g. the very first
    turn before anything is stored). Any failure is swallowed and returns None
    so an inspection artifact never takes down the run itself.
    """
    try:
        text = capture_mod.capture(
            binding.root_uuid, provider.history_provider, binding.root_uuid
        )
    except Exception:  # noqa: BLE001 - capture is non-critical; never fail the loop
        return None
    if not text:
        return None
    path = paths.transcript
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.relative_to(workspace_root).as_posix()


def _operator_fork(binding: Binding, provider: Provider) -> str:
    """Render the managed session for the operator, marker redacted.

    Redaction keeps the operator's own stored session from matching the task's
    marker search (see marker.redact).
    """
    if binding.current is None:
        return ""
    fork = compact_mod.render_fork(binding.root_uuid, provider.history_provider)
    return marker_mod.redact(fork, binding.root_uuid)


def _persist(
    paths: TaskPaths,
    state: TaskState,
    binding: Binding,
    verdict: Verdict,
    result: TurnResult | None,
    phase: str,
) -> None:
    """Apply a verdict to disk. The harness is the only writer.

    Order matters for crash-safety: checklist merge -> binding -> state ->
    ledger (ledger last = committed).
    """
    current = checklist_mod.load_checklist(paths.checklist)
    merged = checklist_mod.merge_update(current, verdict.checklist_update)
    checklist_mod.save_checklist(paths.checklist, merged)
    storage.save_binding(paths, binding)
    storage.save_state(paths, state)
    storage.append_ledger(
        paths,
        LedgerEntry(
            iteration=state.iteration,
            generation=state.generation,
            verdict=verdict.kind,
            exit_code=result.exit_code if result else 0,
            timed_out=result.timed_out if result else False,
            duration_s=result.duration_s if result else 0.0,
            notes=verdict.notes,
            next_prompt=verdict.next_prompt,
            reset_context=verdict.reset_context,
            phase=phase,
        ),
    )


def _report(verdict: Verdict) -> None:
    """Echo the operator's note and queued prompt to stderr for a live watcher."""
    progress.operator_note(verdict.notes)
    progress.next_prompt(verdict.next_prompt)


def _terminal(paths: TaskPaths, state: TaskState, verdict: Verdict) -> TaskResult | None:
    """Return the terminal result a verdict implies, or None to keep going."""
    merged = checklist_mod.load_checklist(paths.checklist)
    if verdict.kind is VerdictKind.DONE or checklist_mod.all_done(merged):
        return _finish(paths, state, TaskStatus.COMPLETE, verdict.kind, verdict.notes)
    if verdict.kind is VerdictKind.ASK_HUMAN:
        return _finish(paths, state, TaskStatus.BLOCKED, verdict.kind, verdict.notes)
    return None


def _new_binding(provider: str) -> Binding:
    return Binding(root_uuid=marker_mod.new_root_uuid(), provider=provider)


def _record_native(binding: Binding, result: TurnResult, provider: Provider) -> Binding:
    """Capture the native session id and transcript size into the current gen.

    The id is taken from the CLI's own output when it printed one, and otherwise
    recovered by searching history for the task's marker -- the authoritative
    path, since the marker is in the transcript whether or not the process got a
    chance to report anything. Size always comes from history: opencode stores
    sessions in SQLite, so there is no file for us to stat.
    """
    if not binding.generations:
        return binding
    gens = list(binding.generations)
    cur = gens[-1]
    native = result.native_session_id or cur.native_id
    size = cur.transcript_bytes

    info = history.find_session(binding.root_uuid, provider.history_provider)
    if info is not None:
        native = native or info.native_id
        size = info.size_bytes

    gens[-1] = replace(cur, native_id=native, transcript_bytes=size, mtime=time.time())
    return replace(binding, generations=gens)


def _refresh_size(binding: Binding, provider: Provider) -> Binding:
    """Re-read the current generation's transcript size at run start.

    ``_record_native`` only refreshes the size after a *managed* turn, so a
    session advanced any other way -- a ``takeover`` the human drove by hand, or
    a crash before the next turn -- leaves the recorded size stale. The first
    resumed turn feeds exactly that number to ``compact_mod.should_compact``, so
    a stale size resumes a bloated session instead of compacting it.

    In memory only; the next ``_persist`` commits it with the rest of the
    binding, so this adds no write outside the crash-safety ordering. Like every
    other history read it degrades quietly: a ``None`` lookup leaves the
    recorded size standing, and a misconfigured ``history_provider`` still
    raises ``HistoryError`` rather than silently disabling compaction.
    """
    if not binding.generations:
        return binding
    info = history.find_session(binding.root_uuid, provider.history_provider)
    if info is None:
        return binding
    gens = list(binding.generations)
    gens[-1] = replace(gens[-1], transcript_bytes=info.size_bytes, mtime=time.time())
    return replace(binding, generations=gens)


def _finish(
    paths,
    state: TaskState,
    status: TaskStatus,
    verdict: VerdictKind | None,
    reason: str,
) -> TaskResult:
    state.status = status
    state.reason = reason
    storage.save_state(paths, state)
    return TaskResult(
        slug=state.slug,
        status=status,
        iterations=state.iteration,
        final_verdict=verdict,
        reason=reason,
    )

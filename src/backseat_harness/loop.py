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

import time
from collections.abc import Callable
from dataclasses import replace

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
# Invokes the operator with an assembled prompt and returns its final text.
OperatorInvoker = Callable[[str], str]


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
    binding = storage.load_binding(paths) or _new_binding(config.provider)
    meta = storage.read_meta(paths)
    task_dir = paths.root.relative_to(config.workspace_root).as_posix()
    paths.docs.mkdir(parents=True, exist_ok=True)
    managed_extra = config.extra_args + (
        ("--model", config.model) if config.model else ()
    ) + (
        ("--agent", config.agent) if config.agent else ()
    )

    def call_operator(outcome: TurnOutcome | None, transcript_file: str | None) -> Verdict:
        verdict = op.review(
            op.ReviewInputs(
                meta_prompt=meta,
                checklist=checklist_mod.load_checklist(paths.checklist),
                managed_fork=_operator_fork(binding, provider),
                outcome=outcome,
                iteration=state.iteration,
                task_dir=task_dir,
                verify_commands=list(config.operator.verify_commands),
                docs=_list_docs(paths, config.workspace_root),
                transcript_file=transcript_file,
            ),
            operator_invoker,
        )
        return verdict or Verdict(kind=VerdictKind.PROBLEM, notes="unparseable verdict")

    started = time.monotonic()
    pending_prompt = first_prompt
    pending_reset = False
    last_verdict: VerdictKind | None = None

    # --- Plan phase: no prompt queued, so the operator drives first. ---
    if pending_prompt is None:
        progress.iteration_start(state.iteration, state.generation, "plan")
        verdict = call_operator(None, None)
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

        # --- Spawn + wait (turn end == process exit). ---
        spec = SpawnSpec(
            prompt=prompt,
            cwd=config.workspace_root,
            resume_native_id=resume_id,
            timeout_s=config.turn_timeout_s,
            extra_args=managed_extra,
        )
        result = managed_runner(provider, spec)
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
        verdict = call_operator(outcome, transcript_file)
        _persist(paths, state, binding, verdict, result, phase="review")
        _report(verdict)
        last_verdict = verdict.kind

        # --- Decide next action. ---
        terminal = _terminal(paths, state, verdict)
        if terminal is not None:
            return terminal
        # continue or problem: repeat with the next prompt (problem repeats naturally).
        pending_prompt = verdict.next_prompt or pending_prompt
        pending_reset = verdict.reset_context


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

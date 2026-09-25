"""The core loop: prep -> spawn -> wait -> assess -> repeat.

``run_task`` is the library entry point a future orchestrator imports. The CLI
(cli.py) is a thin wrapper over it. The loop is the only writer to the task's
``.backseat`` files; every persisted mutation (checklist merge, binding update,
state, ledger) happens here after the system agent's verdict is parsed.

Note: the managed-agent spawn and system-agent invocation are injected so this
module has no hard dependency on a live CLI. cli.py wires the real runners;
tests wire fakes. The wiring functions are intentionally left as thin seams
(``managed_runner`` / ``system_invoker``) rather than being hardcoded, keeping
the loop deterministic and testable.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from backseat_harness import checklist as checklist_mod
from backseat_harness import compact as compact_mod
from backseat_harness import history, storage
from backseat_harness import marker as marker_mod
from backseat_harness import system_agent as sa
from backseat_harness.config import Config
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
# Invokes the system agent with an assembled prompt and returns its final text.
SystemInvoker = Callable[[str], str]


def run_task(
    slug: str,
    config: Config,
    provider: Provider,
    managed_runner: ManagedRunner,
    system_invoker: SystemInvoker,
    first_prompt: str | None = None,
) -> TaskResult:
    """Drive a single task lane to a terminal state.

    ``first_prompt`` seeds iteration 1 when there is no prior verdict; on resume
    the loop derives the next prompt from the system agent instead.
    """
    paths = config.task_paths(slug)
    state = storage.load_state(paths, slug)
    binding = storage.load_binding(paths) or _new_binding(config.provider)
    meta = storage.read_meta(paths)

    started = time.monotonic()
    pending_prompt = first_prompt
    last_verdict: VerdictKind | None = None

    while True:
        # --- Budget guards (cheap, forward-only). ---
        if state.iteration >= config.max_iterations:
            return _finish(paths, state, TaskStatus.FAILED, last_verdict, "max_iterations reached")
        if time.monotonic() - started >= config.max_wall_clock_s:
            return _finish(paths, state, TaskStatus.FAILED, last_verdict, "max_wall_clock reached")
        if pending_prompt is None:
            return _finish(
                paths, state, TaskStatus.FAILED, last_verdict, "no prompt to send"
            )

        state.iteration += 1

        # --- Compaction decision (before spawn). ---
        gen = binding.current
        transcript_bytes = gen.transcript_bytes if gen else 0
        resume_id = gen.native_id if gen else None
        prompt = pending_prompt

        if gen and compact_mod.should_compact(transcript_bytes, config):
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
            extra_args=config.extra_args,
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

        # --- Assess: fresh system agent reviews the turn. ---
        current_checklist = checklist_mod.load_checklist(paths.checklist)
        fork_text = (
            compact_mod.render_fork(binding.root_uuid, provider.history_provider)
            if binding.current
            else ""
        )
        verdict = sa.review(
            sa.ReviewInputs(
                meta_prompt=meta,
                checklist=current_checklist,
                managed_fork=fork_text,
                outcome=outcome,
                iteration=state.iteration,
            ),
            system_invoker,
        )

        if verdict is None:
            verdict = Verdict(kind=VerdictKind.PROBLEM, notes="unparseable verdict")

        # --- Persist (harness is the only writer). Order matters for crash-safety:
        #     checklist merge -> binding -> state -> ledger (ledger last = committed).
        merged = checklist_mod.merge_update(current_checklist, verdict.checklist_update)
        checklist_mod.save_checklist(paths.checklist, merged)
        storage.save_binding(paths, binding)
        storage.save_state(paths, state)
        storage.append_ledger(
            paths,
            LedgerEntry(
                iteration=state.iteration,
                generation=state.generation,
                verdict=verdict.kind,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_s=result.duration_s,
                notes=verdict.notes,
            ),
        )
        last_verdict = verdict.kind

        # --- Decide next action. ---
        if verdict.kind is VerdictKind.DONE or checklist_mod.all_done(merged):
            return _finish(paths, state, TaskStatus.DONE, last_verdict, verdict.notes)
        if verdict.kind is VerdictKind.ASK_HUMAN:
            return _finish(paths, state, TaskStatus.PAUSED, last_verdict, verdict.notes)
        # continue or problem: repeat with the next prompt (problem repeats naturally).
        pending_prompt = verdict.next_prompt or pending_prompt


def _new_binding(provider: str) -> Binding:
    return Binding(root_uuid=marker_mod.new_root_uuid(), provider=provider)


def _record_native(binding: Binding, result: TurnResult, provider: Provider) -> Binding:
    """Capture the native session id and transcript size into the current gen.

    The id is taken from the CLI's own output when it printed one, and otherwise
    recovered by searching history for the task's marker -- the authoritative
    path, since the marker is in the transcript whether or not the process got a
    chance to report anything. Size always comes from history: the v0.1 provider
    stores sessions in SQLite, so there is no file for us to stat.
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
    storage.save_state(paths, state)
    return TaskResult(
        slug=state.slug,
        status=status,
        iterations=state.iteration,
        final_verdict=verdict,
        reason=reason,
    )

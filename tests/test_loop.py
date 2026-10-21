"""End-to-end loop with fake managed + operator agents (no live CLI)."""

import json

import pytest
from conftest import FakeProvider

from backseat_harness import logs as logs_mod
from backseat_harness import marker as marker_mod
from backseat_harness import storage
from backseat_harness.history import SessionInfo
from backseat_harness.loop import OperatorReply, run_task
from backseat_harness.models import (
    Binding,
    Generation,
    TaskState,
    TaskStatus,
    VerdictKind,
)
from backseat_harness.providers.base import SpawnSpec, TurnResult
from backseat_harness.providers.commandcode import CommandCodeProvider
from backseat_harness.providers.kiro import KiroProvider

#: A kiro 2.26.1 refusal, verbatim: fresh and ``--resume-id`` turns both end
#: this way when the id is not in the catalog -- ``runError`` at stage
#: ``prompt`` naming the id, empty stderr, exit 1.
_KIRO_RUNERROR = (
    '{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}'
    '\n'
    '{"type":"metadata","data":{"sessionId":"15742df0-2fe0-4fbd-84c5-fdf924c42dbd","contextUsag'
    'ePercentage":7.193499565124512,"reasoning":{"support":"unavailable","effortLevels":[]}}}'
    '\n'
    '{"type":"runError","data":{"sessionId":"15742df0-2fe0-4fbd-84c5-fdf924c42dbd","stage":"prompt",'
    '"message":"Internal error (code -32603): Encountered an error in the response stream: '
    "The model 'no-such-model-xyz' is not available. Please use '/model' to select a different "
    'model and try again. (request_id: ed60301d-9126-4917-a764-5b1a7aaf72c2)"}}'
    '\n'
)

#: Phrases from the retired advisory. On 2.26.1 the CLI applies --model, so no
#: channel may still assert that it does not (docs 1, "Re-test on kiro-cli
#: 2.26.1"); these guard both channels against the claim coming back.
_RETIRED_KIRO_CLAIM = ("does not honor", "default routing", "never applies")


@pytest.fixture(autouse=True)
def _history(stub_history):
    """Every loop test runs against stubbed chat history, never a live CLI."""
    return stub_history


def _managed_runner_ok(provider, spec):
    return TurnResult(
        exit_code=0, timed_out=False, duration_s=0.1,
        native_session_id="sess-1", stdout="", stderr="",
    )


def _spec_logger(seen):
    """A runner that records each spawn's extra_args, then behaves normally."""

    def runner(prov, spec):
        seen.append(spec.extra_args)
        return _managed_runner_ok(prov, spec)

    return runner


def test_loop_completes_on_done_verdict(config, task):
    slug, paths = task
    provider = FakeProvider()

    verdicts = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {"s1": {"status": "active"}}},
        }),
        json.dumps({
            "verdict": "done",
            "notes": "all set",
            "checklist_update": {"steps": {"s1": {"status": "done"}}},
        }),
    ])

    def operator_invoker(prompt: str) -> str:
        return next(verdicts)

    result = run_task(
        slug, config, provider, _managed_runner_ok, operator_invoker, first_prompt="step 1"
    )

    assert result.status is TaskStatus.COMPLETE
    assert result.iterations == 2
    assert result.final_verdict is VerdictKind.DONE

    # Checklist merged and persisted by the harness.
    data = json.loads(paths.checklist.read_text())
    assert data["steps"]["s1"]["status"] == "done"
    # Two ledger lines, one per iteration.
    entries = list(storage.read_ledger(paths))
    assert len(entries) == 2
    # The ledger records the exact prompt the operator queued next.
    assert entries[0].next_prompt == "step 2"
    assert entries[1].next_prompt is None  # terminal 'done' queues nothing
    # Binding captured the native id.
    assert storage.load_binding(paths).current.native_id == "sess-1"


# --- Persistence order: the ledger lands last (load-bearing invariant) ---


def test_ledger_is_persisted_last(config, task):
    """The ledger's file is updated with the verdict after checklist, binding,
    and state within the turn's persist cycle (load-bearing crash anchor)."""
    slug, paths = task

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"done"}'

    run_task(slug, config, FakeProvider(), _managed_runner_ok, operator_invoker, first_prompt="go")
    # The turn's verdict line lands in the ledger.
    import json
    lines = [line for line in paths.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["verdict"] == "done"
    # The final lane state reflects the terminal result; it is durable.
    state = storage.load_state(paths, slug)
    assert state.status is TaskStatus.COMPLETE


def test_loop_pauses_on_ask_human(config, task):
    slug, paths = task
    provider = FakeProvider()

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"ask_human","notes":"which db?"}'

    result = run_task(
        slug, config, provider, _managed_runner_ok, operator_invoker, first_prompt="go"
    )
    assert result.status is TaskStatus.BLOCKED
    assert result.iterations == 1


def test_owner_brief_and_its_question_reach_the_plan_prompt(config, task):
    """A blocked lane's question, and the owner's answer, ride the same prompt.

    The question comes from state.reason, which run_task overwrites on entry, so
    this is the only place the capture can be wrong: lose it and the operator is
    handed an answer to a question it has no memory of asking.
    """
    slug, paths = task
    storage.save_state(
        paths,
        TaskState(
            slug=slug, status=TaskStatus.BLOCKED, iteration=45,
            reason="D1: split the retry budget, or keep it shared?",
        ),
    )
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        owner_brief="Keep it shared.",
    )
    plan = prompts[0]
    assert "D1: split the retry budget, or keep it shared?" in plan
    assert "Keep it shared." in plan
    assert plan.index("D1: split") < plan.index("Keep it shared.")
    # The run cleared the recorded question, so a lane left `continue` is honest.
    state = storage.load_state(paths, slug)
    assert state.reason == ""
    assert state.status is TaskStatus.COMPLETE


def test_owner_brief_rides_every_call_of_the_run(config, task):
    """Whole-run steering: a review call still reads the brief."""
    slug, paths = task
    prompts: list[str] = []
    calls = {"n": 0}

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"verdict":"continue","next_prompt":"go"}'
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        owner_brief="Sweep the docs too.",
    )
    assert len(prompts) == 2
    assert "Sweep the docs too." in prompts[0]
    assert "Sweep the docs too." in prompts[1]


def test_the_question_rides_review_calls_beside_its_answer(config, task):
    """The pair travels together: an answer with no question is not answerable."""
    slug, paths = task
    storage.save_state(
        paths,
        TaskState(slug=slug, status=TaskStatus.BLOCKED, reason="which db?"),
    )
    prompts: list[str] = []
    calls = {"n": 0}

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"verdict":"continue","next_prompt":"go"}'
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        owner_brief="Use sqlite.",
    )
    assert len(prompts) == 2
    for prompt in prompts:
        assert "which db?" in prompt
        assert "Use sqlite." in prompt


def test_plan_ledger_line_records_the_brief(config, task):
    """The owner's words enter the conversation at the plan line, recorded once."""
    slug, paths = task
    calls = {"n": 0}

    def operator_invoker(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"verdict":"continue","next_prompt":"go"}'
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        owner_brief="Sweep the docs too.",
    )
    entries = list(storage.read_ledger(paths))
    assert entries[0].phase == "plan"
    assert entries[0].owner_brief == "Sweep the docs too."
    # The run's review lines never repeat it: one record per brief.
    assert entries[1].phase == "review"
    assert entries[1].owner_brief is None


def test_no_note_carries_no_question(config, task):
    """A bare resume of a blocked lane has no answer, so nothing is injected."""
    slug, paths = task
    storage.save_state(
        paths,
        TaskState(slug=slug, status=TaskStatus.BLOCKED, reason="which db?"),
    )
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(slug, config, FakeProvider(), _managed_runner_ok, operator_invoker)
    assert "which db?" not in prompts[0]
    assert "Brief from the task owner" not in prompts[0]


def test_note_is_not_captured_for_a_prompt_override(config, task):
    """--prompt skips the plan phase, so nothing could carry the brief to a review."""
    slug, paths = task
    storage.save_state(
        paths,
        TaskState(slug=slug, status=TaskStatus.BLOCKED, reason="which db?"),
    )
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="do the thing",
    )
    # The review after the turn still runs, and carries neither field.
    assert len(prompts) == 1
    assert "which db?" not in prompts[0]
    assert "Brief from the task owner" not in prompts[0]


def test_loop_stops_at_max_iterations(config, task):
    from dataclasses import replace

    slug, paths = task
    config = replace(config, max_iterations=3)
    provider = FakeProvider()

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"continue","next_prompt":"again"}'

    result = run_task(
        slug, config, provider, _managed_runner_ok, operator_invoker, first_prompt="go"
    )
    assert result.status is TaskStatus.CONTINUE
    assert "max_iterations" in result.reason
    assert result.iterations == 3
    assert (
        storage.load_state(paths, slug).reason
        == "max_iterations reached after 3 turns this run (3 total)"
    )


def test_max_iterations_is_a_run_budget_not_a_lane_total(config, task):
    """A lane that already spent turns gets this run's whole budget.

    The regression this pins: budgeting off the durable counter handed a resumed
    lane only what was left of an earlier run's cap, so a lane sitting at 45 of
    the old 50-turn cap got five turns and then a permanent BLOCKED that no brief
    could argue its way past.
    """
    from dataclasses import replace

    slug, paths = task
    storage.save_state(paths, TaskState(slug=slug, iteration=45))
    config = replace(config, max_iterations=2)
    prompts: list[str] = []
    seen: list[object] = []

    def runner(prov, spec):
        seen.append(spec.log_path)
        return _managed_runner_ok(prov, spec)

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"continue","next_prompt":"again"}'

    result = run_task(
        slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go"
    )

    assert result.status is TaskStatus.CONTINUE
    # Two turns spent this run, on top of the 45 the lane already had.
    assert result.iterations == 47
    assert result.reason == "max_iterations reached after 2 turns this run (47 total)"
    # The run-local count never reaches the log names: they come off the durable
    # counter, so this run's files land after the earlier lane's instead of
    # overwriting iter-0001 the way a reset counter would.
    assert [p.name for p in seen] == [
        "iter-0046.managed.log",
        "iter-0047.managed.log",
    ]


def test_first_turn_applies_marker(config, task):
    slug, paths = task
    provider = FakeProvider()
    seen = {}

    def runner(provider, spec):
        seen["prompt"] = spec.prompt
        return _managed_runner_ok(provider, spec)

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"done"}'

    run_task(slug, config, provider, runner, operator_invoker, first_prompt="hello")
    assert seen["prompt"].startswith("<meta>START SESSION")
    assert "hello" in seen["prompt"]


def _killed_then_resumed(
    config, slug, provider, reported="dead-1", *, exit_code=124, timed_out=True
):
    """Run two turns: a turn reporting ``reported``, then the next turn.

    Every turn ends with ``exit_code`` and ``timed_out`` (killed by default). History
    holds no session for the task, so ``reported`` names nothing that persisted.
    Returns the two spawn specs the runner saw.
    """
    seen: list[SpawnSpec] = []

    def runner(prov, spec):
        seen.append(spec)
        return TurnResult(
            exit_code=exit_code, timed_out=timed_out, duration_s=2.0,
            native_session_id=reported if len(seen) == 1 else None,
        )

    verdicts = iter([
        '{"verdict":"continue","next_prompt":"step 2"}',
        '{"verdict":"done"}',
    ])
    run_task(slug, config, provider, runner, lambda p: next(verdicts), first_prompt="step 1")
    return seen


def test_dead_native_id_is_dropped_and_next_turn_is_marked(config, task):
    """L9: a run_start id that history lacks must not poison the next turn.

    The second turn gets no resume id, so cmdc gets no ``--session``, and its
    prompt carries the generation marker so the marker search can bind it.
    """
    slug, paths = task
    from dataclasses import replace

    config = replace(config, provider="commandcode")
    provider = CommandCodeProvider()

    first, second = _killed_then_resumed(config, slug, provider)

    assert first.resume_native_id is None
    assert second.resume_native_id is None
    assert "--session" not in provider.build_command(second)
    assert second.prompt.startswith("<meta>START SESSION")
    assert "step 2" in second.prompt
    assert storage.load_binding(paths).current.native_id is None


def test_failed_turn_with_no_history_session_drops_its_id(config, task):
    """L9, the non-timeout case: a nonzero exit with no history session clears the id."""
    slug, paths = task
    from dataclasses import replace

    config = replace(config, provider="commandcode")
    provider = CommandCodeProvider()

    first, second = _killed_then_resumed(
        config, slug, provider, exit_code=1, timed_out=False
    )

    assert second.resume_native_id is None
    assert second.prompt.startswith("<meta>START SESSION")
    assert storage.load_binding(paths).current.native_id is None


def test_successful_turn_with_no_history_session_keeps_its_id(config, task):
    """A completed turn whose transcript has not reached history keeps its id.

    The CLI may simply not have flushed the session yet, so the next turn resumes
    the reported id and carries no marker.
    """
    slug, paths = task
    from dataclasses import replace

    config = replace(config, provider="commandcode")
    provider = CommandCodeProvider()

    first, second = _killed_then_resumed(
        config, slug, provider, exit_code=0, timed_out=False
    )

    assert second.resume_native_id == "dead-1"
    assert not second.prompt.startswith("<meta>START SESSION")
    assert storage.load_binding(paths).current.native_id == "dead-1"


def test_provider_without_the_capability_keeps_the_dead_id(config, task):
    """Paired control: with the capability off, the same sequence resumes ``dead-1``.

    Claude, kiro and opencode keep today's behaviour: the reported id stands and
    the next turn resumes it, with no marker added.
    """
    slug, paths = task
    provider = FakeProvider()
    assert provider.drops_unpersisted_native_id is False

    first, second = _killed_then_resumed(config, slug, provider)

    assert first.resume_native_id is None
    assert second.resume_native_id == "dead-1"
    assert not second.prompt.startswith("<meta>START SESSION")
    assert storage.load_binding(paths).current.native_id == "dead-1"


def test_provider_without_the_capability_keeps_the_id_after_a_success(config, task):
    """Paired control for the success case: without the capability the id stands."""
    slug, paths = task
    provider = FakeProvider()
    assert provider.drops_unpersisted_native_id is False

    first, second = _killed_then_resumed(
        config, slug, provider, exit_code=0, timed_out=False
    )

    assert second.resume_native_id == "dead-1"
    assert not second.prompt.startswith("<meta>START SESSION")
    assert storage.load_binding(paths).current.native_id == "dead-1"


def test_provider_without_the_capability_gets_no_marker_on_an_id_less_turn(config, task):
    """Paired control for the marker gate (loop.py, the drops_unpersisted check).

    The first turn reports no session id and history holds none, so the next
    turn has no resume id and reaches the gate. With the capability off the
    gate must stay shut: the prompt is not marked. Removing the capability
    check from that gate makes this fail.
    """
    slug, paths = task
    provider = FakeProvider()
    assert provider.drops_unpersisted_native_id is False

    first, second = _killed_then_resumed(config, slug, provider, reported=None)

    assert first.resume_native_id is None
    assert second.resume_native_id is None
    assert second.prompt == "step 2"
    assert storage.load_binding(paths).current.native_id is None


def test_oversized_session_starts_a_new_generation(config, task, _history):
    """Size comes from history (opencode has no transcript file to stat).

    Over the threshold, the next turn must open a fresh session seeded with the
    forked preamble and an incremented ``#n``, not resume the bloated one.
    """
    from dataclasses import replace

    slug, paths = task
    config = replace(config, compact_threshold_bytes=1000)
    provider = FakeProvider()

    _history["fork"] = "PRIOR TRANSCRIPT"
    _history["session"] = SessionInfo(
        key="fake_history:ses_big",
        native_id="ses_big",
        size_bytes=5000,  # over the 1000-byte threshold
        mtime=0.0,
    )

    seen: list[tuple[str, str | None]] = []

    def runner(prov, spec):
        seen.append((spec.prompt, spec.resume_native_id))
        return _managed_runner_ok(prov, spec)

    verdicts = iter([
        '{"verdict":"continue","next_prompt":"step 2"}',
        '{"verdict":"done"}',
    ])

    run_task(slug, config, provider, runner, lambda p: next(verdicts), first_prompt="step 1")

    assert len(seen) == 2
    # Turn 1 establishes generation 1.
    assert "#1" in seen[0][0]
    assert seen[0][1] is None
    # Turn 2 compacts: fresh session (no resume), generation 2, fork preamble,
    # and the real instruction still present.
    second_prompt, second_resume = seen[1]
    assert second_resume is None
    assert "#2" in second_prompt
    assert "PRIOR TRANSCRIPT" in second_prompt
    assert "step 2" in second_prompt
    assert storage.load_state(paths, slug).generation == 2


def test_resume_compacts_on_a_session_the_human_advanced(config, task, _history):
    """The recorded size goes stale when no managed turn did the updating.

    Only ``_record_native`` refreshes ``transcript_bytes``, and it runs after a
    managed turn -- so a ``takeover`` the human drove by hand leaves the binding
    describing the session as it was *before* they touched it. The resumed run
    must re-read the size and compact, not resume the session it has now outgrown.
    """
    from dataclasses import replace

    slug, paths = task
    config = replace(config, compact_threshold_bytes=1000)
    provider = FakeProvider()

    # What the previous run recorded before the takeover: small, resumable.
    storage.save_binding(
        paths,
        Binding(
            root_uuid="root-1",
            provider="fake",
            generations=[Generation(n=1, native_id="ses_old", transcript_bytes=10)],
        ),
    )
    storage.save_state(paths, TaskState(slug=slug, iteration=1, generation=1))

    # What history says now: the human's own turns pushed it over the threshold.
    _history["fork"] = "PRIOR TRANSCRIPT"
    _history["session"] = SessionInfo(
        key="fake_history:ses_old",
        native_id="ses_old",
        size_bytes=5000,
        mtime=0.0,
    )

    seen: list[tuple[str, str | None]] = []

    def runner(prov, spec):
        seen.append((spec.prompt, spec.resume_native_id))
        return _managed_runner_ok(prov, spec)

    verdicts = iter([
        '{"verdict":"continue","next_prompt":"keep going"}',
        '{"verdict":"done"}',
    ])

    run_task(slug, config, provider, runner, lambda p: next(verdicts))

    # The very first resumed turn already compacts: a fresh session seeded with
    # the fork and marker #2, not a resume into the bloated ses_old.
    assert seen[0][1] is None
    assert "#2" in seen[0][0]
    assert "PRIOR TRANSCRIPT" in seen[0][0]
    assert storage.load_binding(paths).current.n == 2


@pytest.mark.parametrize(
    "overrides, expected",
    [
        # A dedicated [harness] model is forwarded to the managed turn as --model.
        ({"model": "anthropic/claude-opus-4"}, ("--model", "anthropic/claude-opus-4")),
        # A dedicated [harness] agent is forwarded to the managed turn as --agent.
        ({"agent": "my-builder"}, ("--agent", "my-builder")),
        # extra_args first, then --model, then --agent (last wins in the CLI).
        (
            {"extra_args": ("--flag", "x"), "model": "m", "agent": "a"},
            ("--flag", "x", "--model", "m", "--agent", "a"),
        ),
    ],
    ids=["model", "agent", "declared-order"],
)
def test_managed_flags_are_forwarded_in_declared_order(config, task, overrides, expected):
    from dataclasses import replace

    slug, _ = task
    config = replace(config, **overrides)
    seen = {}

    def runner(prov, spec):
        seen["extra_args"] = spec.extra_args
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen["extra_args"] == expected


class _NoAgentFlagProvider(FakeProvider):
    """A managed provider whose CLI has no main-session agent flag."""

    agent_flag = None


def test_managed_agent_dropped_for_provider_without_agent_flag(
    config, task, monkeypatch, capsys
):
    """An agent on a provider with no agent flag is dropped with one warning
    per run, across every turn, and never fails the lane."""
    from dataclasses import replace

    from backseat_harness.providers import base

    monkeypatch.setattr(base, "_AGENT_WARNED", set())
    slug, _ = task
    config = replace(config, agent="a", model="m", extra_args=("--flag", "x"))
    seen = []
    verdicts = iter([
        json.dumps({"verdict": "continue", "next_prompt": "step 2"}),
        json.dumps({"verdict": "done"}),
    ])

    result = run_task(
        slug, config, _NoAgentFlagProvider(), _spec_logger(seen),
        lambda p: next(verdicts), first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    assert seen == [("--flag", "x", "--model", "m")] * 2
    assert capsys.readouterr().err.count("no agent flag") == 1


def test_managed_agent_warning_is_keyed_per_provider_and_agent(config, monkeypatch, capsys):
    """Warn-once is per (provider, agent) for the process: a repeat agent on a
    later lane stays silent, a different agent warns again."""
    from dataclasses import replace

    from backseat_harness.providers import base

    monkeypatch.setattr(base, "_AGENT_WARNED", set())
    seen = []
    done = json.dumps({"verdict": "done"})
    for slug, agent in [("lane-1", "a"), ("lane-2", "a"), ("lane-3", "b")]:
        paths = config.task_paths(slug)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.goal.write_text("Build a thing.\n", encoding="utf-8")
        paths.checklist.write_text(json.dumps({"steps": {}, "remarks": []}), encoding="utf-8")

        result = run_task(
            slug, replace(config, agent=agent), _NoAgentFlagProvider(), _spec_logger(seen),
            lambda p: done, first_prompt="go",
        )
        assert result.status is TaskStatus.COMPLETE

    err = capsys.readouterr().err
    assert err.count('ignoring agent "a"') == 1
    assert err.count('ignoring agent "b"') == 1
    assert all("--agent" not in extra for extra in seen)


# --- Per-turn model-slot resolution (docs 2-plan §3) ---


def test_active_step_slot_resolves_and_recomputes_each_turn(config, task):
    """An active step's slot drives the turn's --model, and the checklist is
    re-read every spawn: turn 1 (no steps) runs the default, turn 2 (s1 active
    with a slot) runs the slot's model."""
    from dataclasses import replace

    slug, paths = task
    config = replace(
        config,
        model="cfg-model",
        models={"default": "def-model", "thinker": "think-model"},
    )
    seen: list[tuple] = []

    verdicts = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "thinker"},
            }},
        }),
        '{"verdict":"done"}',
    ])

    def runner(prov, spec):
        seen.append(spec.extra_args)
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner, lambda p: next(verdicts),
        first_prompt="step 1",
    )

    assert seen == [
        ("--model", "def-model"),  # turn 1: no active step -> default binding
        ("--model", "think-model"),  # turn 2: s1 active with slot 'thinker'
    ]


def test_ledger_records_the_asked_for_model_despite_a_slot_rewrite(config, task):
    """The line records the id the harness composed into that turn's --model --
    what it asked for, not the slot as the verdict leaves it: turn 1 asks for
    `fast`, the verdict rewrites s1's slot to `thinker`, and line 1 still says
    `fast-model`."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, models={"fast": "fast-model", "thinker": "think-model"})
    paths.checklist.write_text(
        json.dumps({
            "steps": {"s1": {"status": "active", "model": "fast"}},
            "remarks": [],
        }),
        encoding="utf-8",
    )

    verdicts = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {"s1": {"model": "thinker"}}},
        }),
        '{"verdict":"done"}',
    ])
    seen: list[tuple] = []

    def runner(prov, spec):
        seen.append(spec.extra_args)
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner, lambda p: next(verdicts),
        first_prompt="go",
    )

    assert seen == [
        ("--model", "fast-model"),  # turn 1: s1 active with slot 'fast'
        ("--model", "think-model"),  # turn 2: the rewrite took effect
    ]
    lines = [
        json.loads(line)
        for line in paths.ledger.read_text(encoding="utf-8").splitlines()
    ]
    # The rewrite is on disk -- yet line 1 records the id turn 1 asked for,
    # because the value comes from that turn's composed flags, not a
    # re-resolution after this verdict's merge.
    assert json.loads(paths.checklist.read_text())["steps"]["s1"]["model"] == "thinker"
    assert lines[0]["model"] == "fast-model"
    assert lines[1]["model"] == "think-model"


def test_ledger_model_is_null_when_no_model_was_composed(config, task):
    """No slot, no [models] default, no [harness].model: nothing asked for a
    model, so the line claims none -- the provider's own default is not
    knowable from here."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model=None, models={}, extra_args=())
    specs: list[SpawnSpec] = []

    def runner(prov, spec):
        specs.append(spec)
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert "--model" not in specs[0].extra_args
    line = json.loads(paths.ledger.read_text(encoding="utf-8").splitlines()[0])
    assert line["model"] is None
    (entry,) = list(storage.read_ledger(paths))
    assert entry.model is None


def test_ledger_records_an_owner_passthrough_model(config, task):
    """Nothing resolves through slots or config, but the owner's extra_args
    compose a --model: the line records that id, because it is what the
    harness composed into the turn's flags -- the ask, not what served."""
    from dataclasses import replace

    slug, paths = task
    config = replace(
        config,
        model=None,
        models={},
        extra_args=("--skip-approvals", "--model", "owner-baseline"),
    )
    run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    line = json.loads(paths.ledger.read_text(encoding="utf-8").splitlines()[0])
    assert line["model"] == "owner-baseline"


def test_plan_line_records_null_model_and_the_review_line_the_turns(config, task):
    """A plan decision spawns no turn, so its line is null even with a model
    configured; the review line after the turn names the composed id. Raw JSON
    and LedgerEntry.from_dict agree on both."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model="cfg-model", models={})

    verdicts = iter([
        '{"verdict":"continue","next_prompt":"go"}',
        '{"verdict":"done"}',
    ])
    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, lambda p: next(verdicts)
    )

    raw = [
        json.loads(line)
        for line in paths.ledger.read_text(encoding="utf-8").splitlines()
    ]
    entries = list(storage.read_ledger(paths))
    assert [e.phase for e in entries] == ["plan", "review"]
    assert raw[0]["model"] is None
    assert entries[0].model is None
    assert raw[1]["model"] == "cfg-model"
    assert entries[1].model == "cfg-model"


def test_cancelled_step_does_not_drive_the_model_slot(config, task):
    """A cancelled step is not active, so it cannot supply the turn's model."""
    import json as _json
    from dataclasses import replace as _replace

    slug, paths = task
    cfg = _replace(config, models={"code": "anthropic/claude-opus-4-1"})
    paths.checklist.write_text(
        _json.dumps(
            {
                "steps": {"1-old": {"status": "cancelled", "model": "code"}},
                "remarks": [],
            }
        ),
        encoding="utf-8",
    )
    specs = []

    def runner(provider, spec):
        specs.append(spec)
        return _managed_runner_ok(provider, spec)

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"done"}'

    run_task(slug, cfg, FakeProvider(), runner, operator_invoker, first_prompt="go")
    assert "--model" not in specs[0].extra_args


def test_all_cancelled_checklist_completes_the_lane(config, task):
    """A lane whose every step was declined is finished, not left open."""
    import json as _json

    slug, paths = task
    paths.checklist.write_text(
        _json.dumps({"steps": {"1-x": {"status": "cancelled"}}}), encoding="utf-8"
    )

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"continue","next_prompt":"go"}'

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )
    assert result.status is TaskStatus.COMPLETE
    assert result.iterations == 1


def test_active_step_slot_overrides_and_extra_args_keep_order(config, task):
    """Resolution recomputes the whole extra_args tuple: owner flags first, the
    turn's --model after (E8: the slot append wins over owner's --model), then
    the agent flag."""
    from dataclasses import replace

    slug, paths = task
    config = replace(
        config,
        extra_args=("--flag", "x", "--model", "owner-baseline"),
        agent="my-builder",
        model="cfg-model",
        models={"fast": "fast-model"},
    )
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}},
        "remarks": [],
    }), encoding="utf-8")

    seen = {}

    def runner(prov, spec):
        seen["extra_args"] = spec.extra_args
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen["extra_args"] == (
        "--flag", "x", "--model", "owner-baseline", "--model", "fast-model",
        "--agent", "my-builder",
    )


def test_model_fallback_chain_default_slot_then_config_model(config, task):
    """No active step: [models].default wins; without it config.model; with
    neither the flag is omitted entirely."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, model="cfg-model", models={"default": "def-model"})
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen == [("--model", "def-model")]

    seen.clear()
    config = replace(config, models={})  # no default slot -> falls to config.model
    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen == [("--model", "cfg-model")]

    seen.clear()
    config = replace(config, model=None)  # and without config.model -> no flag
    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen == [()]


def test_multi_active_disagreement_picks_first_and_warns(config, task, capsys):
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {
            "s1": {"status": "active", "model": "first-slot"},
            "s2": {"status": "active", "model": "second-slot"},
        },
        "remarks": [],
    }), encoding="utf-8")
    config = replace(
        config,
        model="cfg-model",
        models={"first-slot": "first-model", "second-slot": "second-model"},
    )
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    # First active in insertion order wins.
    assert seen == [("--model", "first-model")]
    err = capsys.readouterr().err
    assert "s1: 'first-slot'" in err and "s2: 'second-slot'" in err
    assert "disagree" in err


def test_agreeing_multi_active_runs_that_slot_without_warning(
    config, task, capsys
):
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {
            "s1": {"status": "active", "model": "fast"},
            "s2": {"status": "active", "model": "fast"},
        },
        "remarks": [],
    }), encoding="utf-8")
    config = replace(config, models={"fast": "fast-model"})
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen == [("--model", "fast-model")]
    assert "disagree" not in capsys.readouterr().err


# --- Finding 5: the disagreement reaches the operator prompt too (plan §4) ---


def _disagreement(*pairs: tuple[str, str | None]) -> str:
    """The exact sentence the loop composes for these active-step pairs."""
    detail = ", ".join(f"{sid}: {model!r}" for sid, model in pairs)
    return (
        f"active steps disagree on a model slot ({detail}); using the first "
        "active step's slot for this turn"
    )


def test_disagreement_reaches_the_operator_prompt(config, task, capsys):
    """Two active steps naming different slots warn on stderr AND in the
    operator prompt. The deterministic first-active pick is the harness's
    tie-break, not an answer: the operator is the only party who can resolve
    the ambiguity, which is what plan §4 asked for."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {
            "s1": {"status": "active", "model": "fast"},
            "s2": {"status": "active", "model": "slow"},
        },
        "remarks": [],
    }), encoding="utf-8")
    config = replace(
        config, model="cfg-model",
        models={"fast": "fast-model", "slow": "slow-model"},
    )
    seen: list[tuple] = []
    prompts: list[str] = []
    responses = iter([
        '{"verdict":"continue","next_prompt":"step 2"}',
        '{"verdict":"done"}',
    ])

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: (prompts.append(p), next(responses))[1],
        first_prompt="go",
    )
    # First active still wins the turn, unchanged.
    assert seen == [("--model", "fast-model"), ("--model", "fast-model")]
    note = _disagreement(("s1", "fast"), ("s2", "slow"))
    # stderr is byte-for-byte what it was before the fix.
    assert f"[warn] task: {note}" in capsys.readouterr().err
    # And every turn until the checklist agrees -- not one-shot.
    assert len(prompts) == 2
    assert all(f"Slot warning: {note}" in p for p in prompts)


def test_disagreement_reaches_the_plan_phase_prompt(config, task, capsys):
    """The default resume path is where an ambiguous checklist is most likely
    found: two steps left active, the run interrupted. The re-planning operator
    is the one who can fix it, so the same sentence belongs in its prompt."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {
            "s1": {"status": "active", "model": "fast"},
            "s2": {"status": "active", "model": "slow"},
        },
        "remarks": [],
    }), encoding="utf-8")
    config = replace(
        config, model="cfg-model",
        models={"fast": "fast-model", "slow": "slow-model"},
    )
    prompts: list[str] = []

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
    )
    note = _disagreement(("s1", "fast"), ("s2", "slow"))
    assert "Your task: plan" in prompts[0]
    assert f"Slot warning: {note}" in prompts[0]
    assert f"[warn] task: {note}" in capsys.readouterr().err


def test_bad_slot_and_disagreement_both_reach_the_prompt(config, task, capsys):
    """Two causes on one turn must both survive. The disagreement is collected
    before the step's own slot is read, so without accumulation exactly one of
    the two would have been the only thing reported."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {
            "s1": {"status": "active", "model": "ghost"},
            "s2": {"status": "active", "model": "fast"},
        },
        "remarks": [],
    }), encoding="utf-8")
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )
    note = _disagreement(("s1", "ghost"), ("s2", "fast"))
    bad = (
        "step 's1' names model slot 'ghost', which no [models] config "
        "defines; running the default model instead"
    )
    prompt = prompts[-1]
    assert prompt.count("Slot warning:") == 1
    assert f"Slot warning: {note}; {bad}" in prompt
    assert seen == [("--model", "cfg-model")]
    err = capsys.readouterr().err
    assert f"[warn] task: {note}" in err
    assert f"[warn] task: {bad}" in err


# --- [models].default that binds no model id: never silent (both return sites)


@pytest.mark.parametrize(
    "steps",
    [{}, {"s1": {"status": "active"}}],
    ids=["no-active-step", "active-step-without-slot"],
)
def test_unusable_default_binding_composes_harness_model_at_both_return_sites(
    config, task, capsys, steps
):
    """An empty [models].default used to hand back '' and compose no --model at
    all, on a run whose [harness].model was set, with nothing on any channel.
    Now that model is composed and the deviation is named, at both places the
    default binding is read: with no active step, and with an active step naming
    no slot, which falls through to the default. load_config already drops such
    a pair, so this is a Config built directly -- the same door the empty named
    slot had."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({"steps": steps, "remarks": []}), encoding="utf-8")
    config = replace(config, model="cfg-model", models={"default": ""})
    prompts: list[str] = []
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )
    assert seen == [("--model", "cfg-model")]
    warning = (
        "[models] default is '', not a model id; using [harness].model "
        "('cfg-model') instead"
    )
    assert f"[warn] config: {warning}" in capsys.readouterr().err
    assert f"Slot warning: {warning}" in prompts[-1]


def test_usable_default_binding_is_composed_and_quiet(config, task, capsys):
    """The bar for this fix: nothing that came through load_config changes. A
    real [models].default is composed and silent; a Config with no `default`
    key falls back to [harness].model and stays silent, exactly as before. A
    valid named slot on a live step is quiet too, and its Model slots section
    still renders."""
    from dataclasses import replace

    slug, paths = task
    seen: list[tuple] = []
    prompts: list[str] = []

    def one(config):
        run_task(
            slug, config, FakeProvider(), _spec_logger(seen),
            lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
            first_prompt="go",
        )

    one(replace(config, model="cfg-model", models={"default": "models-default"}))
    assert seen == [("--model", "models-default")]
    assert "Slot warning:" not in prompts[-1]
    assert "[warn]" not in capsys.readouterr().err

    one(replace(config, model="cfg-model", models={"fast": "fast-model"}))
    assert seen[-1] == ("--model", "cfg-model")
    assert "Slot warning:" not in prompts[-1]
    assert "[warn]" not in capsys.readouterr().err

    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}}, "remarks": [],
    }), encoding="utf-8")
    one(replace(config, model="cfg-model", models={"fast": "fast-model"}))
    assert seen[-1] == ("--model", "fast-model")
    assert "## Model slots" in prompts[-1]
    assert "Slot warning:" not in prompts[-1]
    assert "[warn]" not in capsys.readouterr().err


def test_non_string_model_treated_as_absent_with_warning(config, task, capsys):
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": 7}},
        "remarks": [],
    }), encoding="utf-8")
    config = replace(config, model="cfg-model")
    seen: list[tuple] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert seen == [("--model", "cfg-model")]  # default binding, slot ignored
    assert "'s1'" in capsys.readouterr().err


def test_slot_with_empty_binding_warns_instead_of_silently_dropping_the_flag(
    config, task, capsys
):
    """A [models] value that is not a model id cannot compose a --model: "" is
    falsy at the spawn site, so the pair resolves to nothing and the step would
    silently run the CLI's own default. Naming such a slot falls back loudly on
    both channels instead. load_config drops these pairs at parse; this pins
    the resolution half for a Config that reached the loop some other way."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}},
        "remarks": [],
    }), encoding="utf-8")
    config = replace(
        config, model="cfg-model", models={"fast": "", "thinker": "t-model"},
    )
    seen: list[tuple] = []
    prompts: list[str] = []

    run_task(
        slug, config, FakeProvider(), _spec_logger(seen),
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )
    # The default binding is composed -- never an empty --model argument.
    assert seen == [("--model", "cfg-model")]
    warning = (
        "step 's1' names model slot 'fast', whose [models] value is '', "
        "not a model id; running the default model instead"
    )
    assert warning in capsys.readouterr().err
    assert f"Slot warning: {warning}" in prompts[-1]
    # A slot that does bind a model is unaffected by its neighbour's.
    assert "- `thinker`" in prompts[-1]


def test_empty_slot_binding_never_reaches_the_prompt_as_usable(config, task, capsys):
    """End to end through the loader: `fast = ""` is dropped at parse, so the
    name never becomes vocabulary -- the prompt neither lists it nor lets a
    verdict set it as a valid slot. A step that still names it is treated
    exactly like a typo'd slot: warned as unknown, on both channels."""
    from backseat_harness.config import load_config

    slug, paths = task
    (config.workspace_root / ".backseat" / "config.toml").write_text(
        '[harness]\nmodel = "cfg-model"\n[models]\nfast = ""\nkeep = "k-model"\n',
        encoding="utf-8",
    )
    loaded = load_config(config.workspace_root, slug)
    assert loaded.models == {"keep": "k-model"}
    capsys.readouterr()  # the parse warning, asserted in test_config

    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}},
        "remarks": [],
    }), encoding="utf-8")
    prompts: list[str] = []
    seen: list[tuple] = []
    responses = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "fast"},
            }},
        }),
        '{"verdict":"done"}',
    ])

    run_task(
        slug, loaded, FakeProvider(), _spec_logger(seen),
        lambda p: (prompts.append(p), next(responses))[1],
        first_prompt="go",
    )

    # Not advertised: the prompt cannot route to a name with no model behind it.
    assert "- `fast`" not in prompts[-1]
    assert "- `keep`" in prompts[-1]
    # And naming it is refused at the source, with the surviving slots listed.
    assert "'fast' on step 's1'" in prompts[1]
    assert "Valid slots: keep" in prompts[1]
    # Never silent, and the flag composed is the default binding.
    assert "which no [models] config defines" in capsys.readouterr().err
    assert seen == [("--model", "cfg-model")]


# --- kiro: never falsely accused, never silently refused ---------------------
# On kiro-cli 2.26.1 the CLI applies --model on fresh and resumed turns
# (docs 1, "Re-test on kiro-cli 2.26.1"), so the old "does not honor --model"
# advisory asserted something measured false. What survives is the refusal
# that is real -- an id the catalog does not have, which kiro quotes back and
# the harness has to name on both channels.


@pytest.mark.parametrize(
    "models, steps, source",
    [
        # A named slot: the complaint names the step and the slot it came from.
        (
            {"fast": "no-such-model-xyz"},
            {"s1": {"status": "active", "model": "fast"}},
            "(step 's1' slot 'fast')",
        ),
        # The default binding: no slot exists to blame, so the complaint names it.
        ({"default": "no-such-model-xyz"}, {}, "([models] default)"),
    ],
    ids=["named-slot", "default-binding"],
)
def test_kiro_rejected_id_is_loud_naming_its_binding(
    config, task, capsys, models, steps, source
):
    """Refused-id branch, fresh turn. A [models] binding naming an id kiro does
    not have must be unmistakable: kiro quotes the id back in its runError, and
    the harness names the binding on stderr and in the operator prompt rather
    than letting the turn end as an unnamed nonzero exit. The remedy ships with
    the complaint, and no retired claim survives on either channel."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, provider="kiro", model="cfg-model", models=models)
    paths.checklist.write_text(json.dumps({"steps": steps, "remarks": []}), encoding="utf-8")
    prompts: list[str] = []
    specs: list[SpawnSpec] = []

    def runner(prov, spec):
        specs.append(spec)
        return TurnResult(
            exit_code=1, timed_out=False, duration_s=0.2,
            native_session_id="sess-1", stdout=_KIRO_RUNERROR, stderr="",
        )

    run_task(
        slug, config, KiroProvider(), runner,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )
    # Fresh turn: the id was still composed onto the argv.
    assert specs[0].resume_native_id is None
    assert specs[0].extra_args[-2:] == ("--model", "no-such-model-xyz")
    loud = f"kiro rejected model id 'no-such-model-xyz' {source}; the turn produced no reply"
    err = capsys.readouterr().err
    assert f"[warn] {loud}" in err
    # The remedy ships with the complaint: where valid ids come from.
    assert "`kiro-cli chat --list-models` lists the ids kiro accepts" in err
    for retired in _RETIRED_KIRO_CLAIM:
        assert retired not in err
    review = prompts[-1]
    assert review.count("Slot warning:") == 1
    assert loud in review  # the complaint lands in the review prompt verbatim
    for retired in _RETIRED_KIRO_CLAIM:
        assert retired not in review


def test_kiro_rejected_id_is_loud_on_the_resumed_turn(config, task, capsys):
    """The same refusal on the resumed path. Turn 1 runs fine and hands the
    session back; turn 2 resumes it with the bad id, and the complaint lands in
    that turn's review prompt -- not the first one."""
    from dataclasses import replace

    slug, paths = task
    config = replace(
        config, provider="kiro", model="cfg-model",
        models={"fast": "no-such-model-xyz"},
    )
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}},
        "remarks": [],
    }), encoding="utf-8")
    prompts: list[str] = []
    specs: list[SpawnSpec] = []

    def runner(prov, spec):
        specs.append(spec)
        if spec.resume_native_id is None:
            return _managed_runner_ok(prov, spec)  # turn 1: fine, id sess-1
        return TurnResult(
            exit_code=1, timed_out=False, duration_s=0.2,
            native_session_id="sess-1", stdout=_KIRO_RUNERROR, stderr="",
        )

    verdicts = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "again",
            "checklist_update": {"steps": {"s1": {"status": "active", "model": "fast"}}},
        }),
        json.dumps({"verdict": "done"}),
    ])
    run_task(
        slug, config, KiroProvider(), runner,
        lambda p: (prompts.append(p), next(verdicts))[1],
        first_prompt="go",
    )
    # Turn 2 resumed the session turn 1 created, still with the bad id.
    assert len(specs) == 2
    assert specs[1].resume_native_id == "sess-1"
    assert specs[1].extra_args[-2:] == ("--model", "no-such-model-xyz")
    loud = (
        "kiro rejected model id 'no-such-model-xyz' (step 's1' slot 'fast'); "
        "the turn produced no reply"
    )
    err = capsys.readouterr().err
    assert f"[warn] {loud}" in err
    assert len(prompts) == 2
    assert loud not in prompts[0]  # the clean turn's review says nothing
    assert loud in prompts[1]
    assert prompts[1].count("Slot warning:") == 1


def test_failed_turn_without_a_refusal_signal_prints_no_rejection(config, task, capsys):
    """Exit 1 alone names nothing. The loop asks the provider what the CLI's
    own words say; a provider with no refusal vocabulary (the base default)
    answers None and the operator sees the ordinary failure, not a complaint
    about a model."""
    slug, _ = task
    prompts: list[str] = []

    def runner(prov, spec):
        return TurnResult(
            exit_code=1, timed_out=False, duration_s=0.2,
            native_session_id="sess-1", stdout="", stderr="cli crashed",
        )

    run_task(
        slug, config, FakeProvider(), runner,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )
    err = capsys.readouterr().err
    assert "rejected model id" not in err
    assert "Slot warning" not in prompts[-1]


# --- Operator prompt carries slot vocabulary + warnings (docs 2-plan §4) ---


def test_operator_prompt_lists_configured_slots(config, task):
    from dataclasses import replace

    slug, _ = task
    config = replace(config, model="m", models={"fast": "f-model", "thinker": "t-model"})
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )
    assert "## Model slots" in prompts[-1]
    assert "- `fast`" in prompts[-1]
    assert "- `thinker`" in prompts[-1]
    # The default binding bullet renders even though no 'default' key was set.
    assert "- `default`" in prompts[-1]


# --- Bad slot in the verdict: reject / re-ask once / strip (docs 2-plan §5a) ---


def test_bad_slot_in_verdict_rejected_and_reasked_once(config, task):
    """A verdict naming an unknown slot is rejected wholesale; the operator is
    re-invoked exactly once with a postscript on the SAME assembled prompt
    naming the field and the valid slots."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    responses = iter([
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {
                "s1": {"status": "done", "model": "ghost"},
            }},
        }),
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {
                "s1": {"status": "done", "model": "fast"},
            }},
        }),
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    # Reject + one re-ask, no more: the clean retry lands inside the budget.
    assert len(prompts) == 2
    assert prompts[1].startswith(prompts[0] + "\n\nYour checklist_update set")
    assert "'ghost' on step 's1'" in prompts[1]
    assert 'Valid slots: fast (or "default"; omit for the default model).' in prompts[1]
    assert "Output the JSON object only, no prose." in prompts[1]
    # The clean retry was persisted verbatim, slot intact.
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    step = data["steps"]["s1"]
    assert {k: v for k, v in step.items() if k != "updated_at"} == {
        "status": "done",
        "model": "fast",
    }
    assert isinstance(step["updated_at"], float)


def test_clean_verdict_is_not_reinvoked(config, task):
    """No slot in the delta, no re-ask: exactly one operator call per turn."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, models={"fast": "fast-model"})
    prompts: list[str] = []

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    assert len(prompts) == 1
    assert "Your checklist_update set" not in prompts[0]


def test_reask_still_bad_accepts_strips_and_warns(config, task, capsys):
    """A re-ask that still names a bad slot is accepted with the model key
    stripped before merge: the bad key never reaches disk, stderr names the
    step ids and values dropped. The budget, not the fault, is what ends it."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    bad_slot = {
        "verdict": "done",
        "checklist_update": {"steps": {"s1": {"status": "done", "model": "phantom"}}},
    }
    responses = iter([
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {
                "s1": {"status": "done", "model": "ghost"},
            }},
        }),
        json.dumps(bad_slot),
        json.dumps(bad_slot),  # budget spent: this one is accepted and stripped
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    # Strike three: accepted anyway (slot hygiene must not couple to turn flow).
    assert result.status is TaskStatus.COMPLETE
    assert len(prompts) == 3  # reject + re-ask + the attempt the budget allows
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    step = data["steps"]["s1"]
    # model key stripped
    assert {k: v for k, v in step.items() if k != "updated_at"} == {"status": "done"}
    assert "model" not in step
    err = capsys.readouterr().err
    assert "still named unknown model slot(s)" in err
    assert "after 3 attempt(s)" in err
    # 'ghost' was rejected outright; only the accepted retry's 'phantom' was dropped.
    assert "'phantom' on step 's1'" in err
    assert "model" not in data["steps"]["s1"]


def test_stripped_slot_residue_rides_the_next_prompt(config, task, capsys):
    """The residue warning is carried across turns inside run_task and surfaces
    once, via the slot_warning channel, in the next operator prompt."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    seen: list[tuple] = []
    responses = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "ghost"},
            }},
        }),
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "ghost"},
            }},
        }),
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "ghost"},
            }},
        }),
        '{"verdict":"done"}',
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    def runner(prov, spec):
        seen.append(spec.extra_args)
        return _managed_runner_ok(prov, spec)

    result = run_task(
        slug, config, FakeProvider(), runner, operator_invoker,
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    assert len(prompts) == 4  # review 1 spent all 3 attempts, then review 2
    # The first prompt was clean; the residue lands on the next one only.
    assert "Slot warning:" not in prompts[0]
    assert (
        "Slot warning: operator verdict still named unknown model slot(s) "
        "after 3 attempt(s), dropping: 'ghost' on step 's1'" in prompts[3]
    )
    # Turn 2 ran the stripped step on the default binding (no slot left).
    assert seen[1] == ("--model", "cfg-model")
    assert "still named unknown model slot(s)" in capsys.readouterr().err


def test_a_budget_of_one_means_no_reask_at_all(config, task, capsys):
    """The budget counts invocations, so 1 is a single call. An owner who does
    not want the harness talking to the operator twice says 1, and gets one.

    The synthesized PROBLEM queues no prompt, so the loop re-sends the same one
    and the turn-level repeat cap is what finally stops it -- the two guards are
    separate, and only the inner one is switched off here.
    """
    from dataclasses import replace

    slug, _ = task
    config = replace(config, operator_retry_budget=1)
    prompts: list[str] = []

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: (prompts.append(p), "no json here")[1],
        first_prompt="go",
    )

    # One operator call per iteration; no re-ask inside any of them.
    assert len(prompts) == config.problem_repeat_cap
    assert result.status is TaskStatus.BLOCKED
    assert "problem stuck" in result.reason
    err = capsys.readouterr().err
    assert "operator produced no parsable verdict on 1 attempt(s)" in err
    assert "retrying" not in err


def test_a_nonsense_budget_still_makes_one_attempt(config, task):
    """A budget below 1 is a typo, not a request to skip the operator. Clamping
    to one attempt is the only reading that keeps the loop honest."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, operator_retry_budget=0)
    prompts: list[str] = []

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
        first_prompt="go",
    )

    assert len(prompts) == 1


@pytest.mark.parametrize(
    "first_reply, must_say, must_not_say",
    [
        # An empty reply from a call that was killed: the timeout story is the
        # correction, and the ceiling it blew is named so the fix is actionable.
        (
            OperatorReply("", timed_out=True),
            ["was empty", "TIMEOUTs timeout", "process tree was terminated"],
            ["no parsable verdict JSON"],
        ),
        # An empty reply from a call that ran to its end: no kill claim, no
        # timeout advice; the real fault (no text came back) is named instead.
        (
            "",
            ["was empty", "so nothing was killed, but no text came back"],
            [
                "TIMEOUTs timeout",
                "process tree was terminated",
                "validation tool calls",
                "no parsable verdict JSON",
            ],
        ),
        # Text that is not JSON: the re-ask names the JSON problem only.
        (
            "I could not decide.",
            ["contained no parsable verdict JSON"],
            ["was empty"],
        ),
    ],
    ids=["timed-out-empty", "completed-empty", "unreadable"],
)
def test_a_bad_operator_reply_is_re_asked_with_the_fault_it_actually_had(
    config, task, first_reply, must_say, must_not_say
):
    """An empty reply and an unreadable one are different faults. The operator
    that gets re-asked is a fresh invocation with no memory of the last, so the
    re-ask has to say which fault it was, and only the true one."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, models={"fast": "fast-model"})
    timeout = str(config.operator.timeout_s)
    prompts: list[str] = []
    responses = iter([first_reply, '{"verdict":"done"}'])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert len(prompts) == 2
    retried = prompts[1]
    for text in must_say:
        assert text.replace("TIMEOUT", timeout) in retried
    for text in must_not_say:
        assert text.replace("TIMEOUT", timeout) not in retried


def test_retry_budget_is_shared_and_accumulates_complaints(config, task, capsys):
    """One budget for both faults, and each re-ask names every complaint so far.

    The operator first answers with no JSON, then with a bad slot. The third
    prompt must carry both corrections, because the retried operator has no
    memory of the calls before it.
    """
    from dataclasses import replace

    slug, _ = task
    config = replace(config, models={"fast": "fast-model"})
    prompts: list[str] = []
    responses = iter([
        "this is prose with no json",  # unparsable
        json.dumps({                    # parses, but names a bad slot
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "ghost"},
            }},
        }),
        json.dumps({                    # clean slot retry -> accepted
            "verdict": "done",
            "checklist_update": {"steps": {"s1": {"status": "done"}}},
        }),
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert result.status is TaskStatus.COMPLETE
    assert len(prompts) == 3
    # Every re-ask is the same base prompt plus its accumulated complaints.
    for reask in prompts[1:]:
        assert reask.startswith(prompts[0] + "\n\n")
        assert reask.endswith("Output the JSON object only, no prose.")
    assert "no parsable verdict JSON" in prompts[1]
    assert "ghost" not in prompts[1]
    assert "'ghost' on step 's1'" in prompts[2]
    assert "no parsable verdict JSON" in prompts[2]  # the first fault still stands
    # One line per retry, naming which fault it was.
    err = capsys.readouterr().err
    assert "operator attempt 1/3 was unparsable; retrying" in err
    assert "operator attempt 2/3 was a bad model slot; retrying" in err


def test_plan_phase_bad_slot_is_rejected_too(config, task):
    """§5a wraps both call sites: the plan phase is where the operator first
    writes steps, so a bad slot there is rejected before any persist."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, models={"fast": "fast-model"})
    prompts: list[str] = []
    responses = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "research the router",
            "checklist_update": {"steps": {
                "research": {"title": "Find it", "status": "active", "model": "ghost"},
            }},
        }),
        json.dumps({
            "verdict": "continue",
            "next_prompt": "research the router",
            "checklist_update": {"steps": {
                "research": {"title": "Find it", "status": "active", "model": "fast"},
            }},
        }),
        '{"verdict":"done"}',
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(slug, config, FakeProvider(), _managed_runner_ok, operator_invoker)

    assert result.status is TaskStatus.COMPLETE
    assert "Your task: plan" in prompts[0]
    assert prompts[1].startswith(prompts[0] + "\n\nYour checklist_update set")
    assert "'ghost' on step 'research'" in prompts[1]
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    assert data["steps"]["research"]["model"] == "fast"


def test_plan_phase_warns_on_both_channels_for_an_on_disk_bad_slot(
    config, task, capsys
):
    """§5b in the plan phase: this is the default resume path (first_prompt
    defaults to None) and its operator is the one whose job is to correct the
    checklist, so an on-disk bad slot has to reach *this* prompt -- the review
    phase meeting all three requirements is no help to a run that never gets
    there. Both channels: a stderr [warn] and a Slot warning line. The turn
    still runs on the default model, and the Model slots section renders."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "ghost"}},
        "remarks": [],
    }), encoding="utf-8")
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    seen: list[tuple] = []
    responses = iter([
        json.dumps({"verdict": "continue", "next_prompt": "go"}),
        '{"verdict":"done"}',
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(slug, config, FakeProvider(), _spec_logger(seen), operator_invoker)

    assert result.status is TaskStatus.COMPLETE
    plan_prompt = prompts[0]
    assert "Your task: plan" in plan_prompt  # this really is the plan phase
    assert "## Model slots" in plan_prompt
    assert (
        "Slot warning: step 's1' names model slot 'ghost', which no [models] "
        "config defines; running the default model instead"
    ) in plan_prompt
    assert seen == [("--model", "cfg-model")]
    assert "which no [models] config defines" in capsys.readouterr().err
    # One-shot, not carried: the review phase re-resolves from disk instead of
    # stacking a second copy of the same sentence onto its prompt.
    assert plan_prompt.count("Slot warning:") == 1
    assert prompts[1].count("Slot warning:") == 1


def test_clean_plan_phase_carries_no_slot_warning(config, task, capsys):
    """Negative control for the above: a plan phase with a good slot on disk
    resolves cleanly and must stay quiet on both channels."""
    from dataclasses import replace

    slug, paths = task
    paths.checklist.write_text(json.dumps({
        "steps": {"s1": {"status": "active", "model": "fast"}},
        "remarks": [],
    }), encoding="utf-8")
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok,
        lambda p: (prompts.append(p), '{"verdict":"done"}')[1],
    )

    assert "Your task: plan" in prompts[0]
    assert "Slot warning:" not in prompts[0]
    assert "names model slot" not in capsys.readouterr().err


def test_verdict_naming_default_is_valid_without_a_default_key(config, task, capsys):
    """Plan §1: naming "default" is the same as omitting the field, so it is
    accepted even when [models] defines no default key — no re-ask, no strip,
    and the on-disk step resolves silently to the default binding."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, model="cfg-model", models={"fast": "fast-model"})
    prompts: list[str] = []
    seen: list[tuple] = []
    responses = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "step 2",
            "checklist_update": {"steps": {
                "s1": {"status": "active", "model": "default"},
            }},
        }),
        '{"verdict":"done"}',
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    def runner(prov, spec):
        seen.append(spec.extra_args)
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner, operator_invoker,
        first_prompt="go",
    )

    # No reject/re-ask was fired for the literal default name.
    assert len(prompts) == 2
    assert all("Your checklist_update set" not in p for p in prompts)
    # Merged verbatim, not stripped.
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    assert data["steps"]["s1"]["model"] == "default"
    # Turn 2 resolved through the default binding silently.
    assert seen[1] == ("--model", "cfg-model")
    err = capsys.readouterr().err
    assert "names model slot" not in err
    assert "not a configured slot" not in err


def test_unparseable_reask_synthesizes_problem_without_persisting(config, task):
    """Spending the budget without a usable verdict follows the loop's standing
    unparseable convention: PROBLEM is synthesized, nothing is persisted, and
    the notes name how many attempts were spent and on what — otherwise the next
    operator call sees a bare 'problem' with no cause."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, models={"fast": "fast-model"}, max_iterations=1)
    prompts: list[str] = []
    responses = iter([
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {"s1": {"status": "done", "model": "ghost"}}},
        }),
        "still no json here",
        "and still none here",
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert len(prompts) == 3  # the whole budget, on the original prompt
    assert result.status is TaskStatus.CONTINUE
    assert "max_iterations" in result.reason
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    assert data["steps"] == {}  # no verdict was ever accepted
    # The cause reaches the ledger, naming both faults the budget was spent on.
    ledger = paths.ledger.read_text(encoding="utf-8")
    assert "operator produced no parsable verdict on 3 attempt(s)" in ledger
    assert "unparsable" in ledger
    # The third prompt still carries attempt 1's complaint: the retried operator
    # has no memory of the calls before it, so a stale fault is not withdrawn.
    assert "no parsable verdict JSON" in prompts[2]
    assert "'ghost' on step 's1'" in prompts[2]


def test_non_string_slot_in_verdict_is_bad_and_strips(config, task):
    """§5a literal rule: any present value that is not an existing slot name is
    bad — a JSON number where a slot belongs is rejected, then stripped."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, models={"fast": "fast-model"})
    prompts: list[str] = []
    responses = iter([
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {"s1": {"status": "done", "model": 7}}},
        }),
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {"s1": {"status": "done", "model": 7}}},
        }),
        json.dumps({
            "verdict": "done",
            "checklist_update": {"steps": {"s1": {"status": "done", "model": 7}}},
        }),
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert len(prompts) == 3
    assert "7 on step 's1'" in prompts[1]  # a JSON number, rendered as sent
    data = json.loads(paths.checklist.read_text(encoding="utf-8"))
    step = data["steps"]["s1"]
    # the 7 never hit disk
    assert {k: v for k, v in step.items() if k != "updated_at"} == {"status": "done"}


def _models(config, models):
    from dataclasses import replace

    return replace(config, models=models)


def _kiro(config):
    from dataclasses import replace

    return replace(config, provider="kiro")


# --- PROBLEM re-send bounding (docs 2-plan §6) ---


def test_problem_repeat_cap_blocks_before_max_iterations(config, task):
    """A PROBLEM with no next_prompt re-sends the same prompt; the cap ends the
    run at the Nth, long before max_iterations could."""
    from dataclasses import replace

    slug, paths = task
    config = replace(config, max_iterations=50, problem_repeat_cap=3)
    prompts: list[str] = []

    def runner(prov, spec):
        prompts.append(spec.prompt)
        return _managed_runner_ok(prov, spec)

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"problem","next_prompt":null,"notes":"that broke it"}'

    result = run_task(
        slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go"
    )

    assert result.status is TaskStatus.BLOCKED
    assert result.reason == "problem stuck: same prompt resent 3 turns in a row"
    # Three turns ran, not 50: the cap fired first.
    assert result.iterations == 3
    assert "max_iterations" not in result.reason
    # Every turn re-sent the same prompt, which is what was counted. The cap
    # keys on the *effective* next prompt, so it is unaffected by the marker the
    # first turn prepends -- hence endswith rather than equality here.
    assert len(prompts) == 3
    assert all(p.endswith("go") for p in prompts)
    assert prompts[1] == prompts[2]
    # The blocking verdict is still on disk and ledgered: the cap is a loop
    # bound, not a refusal to record what the operator said.
    assert storage.load_state(paths, slug).reason == result.reason
    assert len(list(storage.read_ledger(paths))) == 3


def test_problem_repeat_cap_resets_on_continue(config, task):
    """Two stuck PROBLEMs, then a continue: the count starts over, so the run
    survives well past what an unbroken run of PROBLEMs would allow."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, max_iterations=50, problem_repeat_cap=3)
    prompts: list[str] = []

    def runner(prov, spec):
        prompts.append(spec.prompt)
        return _managed_runner_ok(prov, spec)

    responses = iter([
        '{"verdict":"problem","next_prompt":null}',   # repeat 1
        '{"verdict":"problem","next_prompt":null}',   # repeat 2
        '{"verdict":"continue","next_prompt":"new plan"}',  # resets
        '{"verdict":"problem","next_prompt":null}',   # repeat 1 again
        '{"verdict":"problem","next_prompt":null}',   # repeat 2 again
        '{"verdict":"problem","next_prompt":null}',   # repeat 3 -> blocked
        '{"verdict":"done"}',                          # must never be reached
    ])

    def operator_invoker(prompt: str) -> str:
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go"
    )

    assert result.status is TaskStatus.BLOCKED
    assert result.reason == "problem stuck: same prompt resent 3 turns in a row"
    # Six turns: 2 blocked, the continue reset, 3 more. Had the counter not
    # reset, the run would have ended at turn 3.
    assert result.iterations == 6
    # The continue really did change what was sent, so the later PROBLEMs were
    # repeats of "new plan", not of the original prompt. Turn 1's spawned text
    # carries the marker (the cap counts the effective prompt, not that).
    assert prompts[0].endswith("go")
    assert prompts[1] == prompts[2] == "go"
    assert prompts[3] == prompts[4] == prompts[5] == "new plan"


def test_problem_repeat_cap_resets_on_a_differing_prompt(config, task):
    """A PROBLEM that names a *new* prompt is recovery, not repetition: it
    breaks the chain even though the verdict kind is unchanged."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, max_iterations=50, problem_repeat_cap=3)
    prompts: list[str] = []

    def runner(prov, spec):
        prompts.append(spec.prompt)
        return _managed_runner_ok(prov, spec)

    responses = iter([
        '{"verdict":"problem","next_prompt":null}',                # repeat 1
        '{"verdict":"problem","next_prompt":"try this instead"}',  # differs -> reset
        '{"verdict":"problem","next_prompt":null}',                # repeat 1 again
        '{"verdict":"problem","next_prompt":null}',                # repeat 2 again
        '{"verdict":"problem","next_prompt":null}',                # repeat 3
        '{"verdict":"done"}',                                      # unreachable
    ])

    def operator_invoker(prompt: str) -> str:
        return next(responses)

    result = run_task(
        slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go"
    )

    assert result.status is TaskStatus.BLOCKED
    assert result.reason == "problem stuck: same prompt resent 3 turns in a row"
    # Five turns: one repeat, then the differing PROBLEM reset the count, then
    # three more. The differing PROBLEM has to come *after* a repeat, or the
    # counter is already 0 and this proves nothing about resetting.
    assert result.iterations == 5
    assert prompts[0].endswith("go")
    assert prompts[1] == "go"
    assert prompts[2] == prompts[3] == prompts[4] == "try this instead"


@pytest.mark.parametrize("cap", [0, -2], ids=["zero", "negative"])
def test_problem_repeat_cap_zero_disables_and_max_iterations_governs(
    config, task, fake_home, capsys, cap
):
    """A disabled cap (0, or a typo'd negative that warns) is today's behavior: an
    unbounded run of identical-prompt PROBLEMs is governed by max_iterations.

    Loaded from real TOML rather than a ``replace``, so the warning and the run
    behavior are pinned on the same path a typo actually takes.
    """
    from dataclasses import replace

    from backseat_harness.config import load_config

    slug, _ = task
    ws = config.workspace_root
    (ws / ".backseat" / "config.toml").write_text(
        f"[harness]\nproblem_repeat_cap = {cap}\nmax_iterations = 4\n", encoding="utf-8"
    )
    resolved = replace(load_config(ws, slug, home=fake_home), provider="fake")

    assert resolved.problem_repeat_cap == cap
    if cap < 0:
        err = capsys.readouterr().err
        assert f"[warn] config: [harness] problem_repeat_cap = {cap} is negative" in err

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"problem","next_prompt":null}'

    result = run_task(
        slug, resolved, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert result.status is TaskStatus.CONTINUE
    assert result.reason == "max_iterations reached after 4 turns this run (4 total)"
    assert result.iterations == 4


def test_unparseable_verdicts_are_bounded_by_the_repeat_cap(config, task):
    """The synthesized PROBLEM for an unparseable verdict re-sends the identical
    prompt by definition -- exactly the shape the cap names, so the operator
    answering with prose forever ends the run instead of the max_iterations."""
    from dataclasses import replace

    slug, _ = task
    config = replace(config, max_iterations=50, problem_repeat_cap=3)

    def operator_invoker(prompt: str) -> str:
        return "I would rather not answer in JSON today."

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert result.status is TaskStatus.BLOCKED
    assert result.reason == "problem stuck: same prompt resent 3 turns in a row"
    assert result.iterations == 3


def test_default_problem_repeat_cap_is_three(config, task):
    """The cap is on by default -- a task that never configures it still gets
    the bound rather than today's unbounded re-send."""
    slug, _ = task

    assert config.problem_repeat_cap == 3

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"problem","next_prompt":null}'

    result = run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert result.reason == "problem stuck: same prompt resent 3 turns in a row"
    assert result.iterations == 3


def test_reset_context_abandons_session(config, task):
    """reset_context starts a fresh session: new generation, no fork, no resume."""
    slug, paths = task
    provider = FakeProvider()

    seen: list[tuple[str, str | None]] = []

    def runner(prov, spec):
        seen.append((spec.prompt, spec.resume_native_id))
        return _managed_runner_ok(prov, spec)

    verdicts = iter([
        json.dumps({"verdict": "continue", "next_prompt": "step 2", "reset_context": True}),
        '{"verdict":"done"}',
    ])

    run_task(slug, config, provider, runner, lambda p: next(verdicts), first_prompt="step 1")

    assert len(seen) == 2
    # Turn 1 establishes generation 1 (no prior session to resume).
    assert "#1" in seen[0][0]
    assert seen[0][1] is None
    # Turn 2 resets: fresh session, generation 2, no carried fork or resume.
    second_prompt, second_resume = seen[1]
    assert second_resume is None
    assert "#2" in second_prompt
    assert "step 2" in second_prompt
    assert storage.load_state(paths, slug).generation == 2


def test_new_task_plans_before_first_turn(config, task):
    """No first_prompt: the operator plans the checklist and writes turn 1."""
    slug, paths = task
    provider = FakeProvider()
    prompts: list[str] = []
    seen_managed: list[str] = []

    verdicts = iter([
        json.dumps({
            "verdict": "continue",
            "next_prompt": "research the router",
            "checklist_update": {"steps": {
                "research": {"title": "Find the router", "status": "active"},
                "route": {"title": "Add the route"},
            }},
            "notes": "planned",
        }),
        '{"verdict":"ask_human","notes":"which framework?"}',
    ])

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(verdicts)

    def runner(prov, spec):
        seen_managed.append(spec.prompt)
        return _managed_runner_ok(prov, spec)

    result = run_task(slug, config, provider, runner, operator_invoker)

    assert result.status is TaskStatus.BLOCKED
    assert result.iterations == 1
    # First operator call is the plan phase, second reviews turn 1.
    assert "Your task: plan" in prompts[0]
    assert "Your task: review managed turn 1" in prompts[1]
    # The planned prompt drove the first managed turn, marker applied.
    assert seen_managed[0].startswith("<meta>START SESSION")
    assert "research the router" in seen_managed[0]
    # The planned checklist was persisted with titles.
    data = json.loads(paths.checklist.read_text())
    step = data["steps"]["route"]
    assert {k: v for k, v in step.items() if k != "updated_at"} == {
        "title": "Add the route",
        "status": "pending",
    }
    phases = [e.phase for e in storage.read_ledger(paths)]
    assert phases == ["plan", "review"]


def test_plan_phase_can_pause_without_spawning(config, task):
    slug, paths = task

    def runner(prov, spec):  # pragma: no cover - must not be called
        raise AssertionError("managed agent must not run")

    result = run_task(
        slug, config, FakeProvider(), runner,
        lambda p: '{"verdict":"ask_human","notes":"goal is ambiguous"}',
    )
    assert result.status is TaskStatus.BLOCKED
    assert result.iterations == 0
    assert result.reason == "goal is ambiguous"


def test_operator_fork_has_marker_redacted(config, task, _history):
    """The operator must never see the root UUID, or its own stored session
    would match the task's marker search."""
    slug, paths = task
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    def runner(prov, spec):
        from backseat_harness.marker import parse_marker

        # The binding isn't saved until after the turn; read the uuid off the
        # marker and make the stubbed fork carry it, as a real transcript would.
        uuid, _ = parse_marker(spec.prompt)
        _history["fork"] = f"<meta>START SESSION {uuid} #1</meta>\nwork done"
        _history["uuid"] = uuid
        return _managed_runner_ok(prov, spec)

    run_task(slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go")

    review_prompt = prompts[-1]
    assert "work done" in review_prompt
    assert _history["uuid"] not in review_prompt
    assert "<task-root>" in review_prompt


def test_loop_creates_docs_dir_and_lists_docs(config, task):
    slug, paths = task
    prompts: list[str] = []

    def runner(prov, spec):
        # The managed agent writes a handoff doc during its turn.
        (paths.docs / "1-research.md").write_text("findings", encoding="utf-8")
        return _managed_runner_ok(prov, spec)

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(slug, config, FakeProvider(), runner, operator_invoker, first_prompt="go")
    assert paths.docs.is_dir()
    assert "docs/1-research.md`" in prompts[-1]


# --- Provider call logs -------------------------------------------------


def test_each_managed_turn_gets_its_own_log_named_for_the_iteration(
    config, task, stub_history
):
    """One file per call, named before the call so a hung turn is tailable."""
    slug, paths = task
    seen: list[object] = []

    def runner(prov, spec):
        seen.append(spec.log_path)
        return _managed_runner_ok(prov, spec)

    run_task(
        slug, config, FakeProvider(), runner,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )

    assert len(seen) == 1
    log = seen[0]
    assert log.name == "iter-0001.managed.log"
    assert log.parent == paths.logs


def test_a_turn_log_is_written_by_the_runner_and_redacted_afterwards(
    config, task, stub_history
):
    """The log is written by the provider call and redacted once it is done.

    The UUID must never reach disk, but the session id has to be parsed out of
    the raw text first -- so redaction runs after the turn, not before it.
    """
    slug, paths = task
    root_uuid = None

    def runner(prov, spec):
        # Stand in for the CLI writing its event stream to the descriptor.
        spec.log_path.write_text(
            '{"sessionID":"ses_from_log"}\n' f"text {root_uuid}\n", encoding="utf-8"
        )
        return TurnResult(
            exit_code=0, timed_out=False, duration_s=0.1,
            native_session_id="ses_from_log", stdout="", stderr="",
        )

    def operator_invoker(prompt: str) -> str:
        return '{"verdict":"done"}'

    # Learn the lane's root uuid from the marker the loop puts on the prompt.
    def capture_prompt(prov, spec):
        nonlocal root_uuid
        root_uuid = marker_mod.parse_marker(spec.prompt)[0]
        return runner(prov, spec)

    run_task(slug, config, FakeProvider(), capture_prompt, operator_invoker, first_prompt="go")

    text = (paths.logs / "iter-0001.managed.log").read_text(encoding="utf-8")
    assert root_uuid not in text
    assert marker_mod.REDACTED_ROOT in text
    assert "ses_from_log" in text  # the id parsed fine; only the UUID went away


def test_every_operator_attempt_keeps_its_own_log(config, task, stub_history):
    """A re-asked verdict must leave the unusable answer on disk.

    Overwriting would destroy the one artifact that explains *why* the harness
    re-asked, which is the whole reason the operator's output is kept at all.
    """
    slug, paths = task
    # One iteration only: the operator here never produces a verdict, so the
    # loop would otherwise keep re-asking across iterations and this test would
    # be asserting on a second iteration's files too.
    from dataclasses import replace as dataclass_replace

    config = dataclass_replace(config, max_iterations=1)
    attempted: list[object] = []

    def operator_invoker(prompt: str) -> str:
        path = logs_mod.current()
        attempted.append(path)
        path.write_text("no verdict here", encoding="utf-8")
        return "still no verdict here"

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )

    assert [p.name for p in attempted] == [
        "iter-0001.operator.1.log",
        "iter-0001.operator.2.log",
        "iter-0001.operator.3.log",
    ]
    # Default budget is three invocations, so three files, none clobbered.
    for path in attempted:
        assert path.read_text(encoding="utf-8") == "no verdict here"


def test_the_loop_releases_the_declared_log_after_each_operator_call(
    config, task, stub_history
):
    """A stale declaration would send an unrelated call's output to a log that
    is already finished -- and, worse, name an iteration that never ran."""
    slug, _ = task
    held: list[object] = []

    def operator_invoker(prompt: str) -> str:
        held.append(logs_mod.current())
        return '{"verdict":"done"}'

    run_task(
        slug, config, FakeProvider(), _managed_runner_ok, operator_invoker,
        first_prompt="go",
    )
    assert held[0] is not None
    assert logs_mod.current() is None
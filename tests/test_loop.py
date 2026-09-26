"""End-to-end loop with fake managed + system agents (no live CLI)."""

import json

import pytest
from conftest import FakeProvider

from backseat_harness import storage
from backseat_harness.history import SessionInfo
from backseat_harness.loop import run_task
from backseat_harness.models import TaskStatus, VerdictKind
from backseat_harness.providers.base import TurnResult


@pytest.fixture(autouse=True)
def _history(stub_history):
    """Every loop test runs against stubbed chat history, never a live CLI."""
    return stub_history


def _managed_runner_ok(provider, spec):
    return TurnResult(
        exit_code=0, timed_out=False, duration_s=0.1,
        native_session_id="sess-1", stdout="", stderr="",
    )


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

    def system_invoker(prompt: str) -> str:
        return next(verdicts)

    result = run_task(
        slug, config, provider, _managed_runner_ok, system_invoker, first_prompt="step 1"
    )

    assert result.status is TaskStatus.DONE
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


def test_loop_pauses_on_ask_human(config, task):
    slug, paths = task
    provider = FakeProvider()

    def system_invoker(prompt: str) -> str:
        return '{"verdict":"ask_human","notes":"which db?"}'

    result = run_task(slug, config, provider, _managed_runner_ok, system_invoker, first_prompt="go")
    assert result.status is TaskStatus.PAUSED
    assert result.iterations == 1


def test_loop_stops_at_max_iterations(config, task):
    from dataclasses import replace

    slug, paths = task
    config = replace(config, max_iterations=3)
    provider = FakeProvider()

    def system_invoker(prompt: str) -> str:
        return '{"verdict":"continue","next_prompt":"again"}'

    result = run_task(slug, config, provider, _managed_runner_ok, system_invoker, first_prompt="go")
    assert result.status is TaskStatus.FAILED
    assert "max_iterations" in result.reason
    assert result.iterations == 3


def test_first_turn_applies_marker(config, task):
    slug, paths = task
    provider = FakeProvider()
    seen = {}

    def runner(provider, spec):
        seen["prompt"] = spec.prompt
        return _managed_runner_ok(provider, spec)

    def system_invoker(prompt: str) -> str:
        return '{"verdict":"done"}'

    run_task(slug, config, provider, runner, system_invoker, first_prompt="hello")
    assert seen["prompt"].startswith("<meta>START SESSION")
    assert "hello" in seen["prompt"]


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


def test_new_task_plans_before_first_turn(config, task):
    """No first_prompt: the system agent plans the checklist and writes turn 1."""
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

    def system_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return next(verdicts)

    def runner(prov, spec):
        seen_managed.append(spec.prompt)
        return _managed_runner_ok(prov, spec)

    result = run_task(slug, config, provider, runner, system_invoker)

    assert result.status is TaskStatus.PAUSED
    assert result.iterations == 1
    # First system call is the plan phase, second reviews turn 1.
    assert "Your task: plan" in prompts[0]
    assert "Your task: review managed turn 1" in prompts[1]
    # The planned prompt drove the first managed turn, marker applied.
    assert seen_managed[0].startswith("<meta>START SESSION")
    assert "research the router" in seen_managed[0]
    # The planned checklist was persisted with titles.
    data = json.loads(paths.checklist.read_text())
    assert data["steps"]["route"] == {"title": "Add the route", "status": "pending"}
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
    assert result.status is TaskStatus.PAUSED
    assert result.iterations == 0
    assert result.reason == "goal is ambiguous"


def test_operator_fork_has_marker_redacted(config, task, _history):
    """The operator must never see the root UUID, or its own stored session
    would match the task's marker search."""
    slug, paths = task
    prompts: list[str] = []

    def system_invoker(prompt: str) -> str:
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

    run_task(slug, config, FakeProvider(), runner, system_invoker, first_prompt="go")

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

    def system_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    run_task(slug, config, FakeProvider(), runner, system_invoker, first_prompt="go")
    assert paths.docs.is_dir()
    assert "docs/1-research.md`" in prompts[-1]

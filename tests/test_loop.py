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
    assert len(list(storage.read_ledger(paths))) == 2
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

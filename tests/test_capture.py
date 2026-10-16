"""Full-session transcript capture: redaction and the loop wiring."""

import pytest
from conftest import FakeProvider

from backseat_harness import capture
from backseat_harness.loop import run_task
from backseat_harness.marker import REDACTED_ROOT
from backseat_harness.models import TaskStatus
from backseat_harness.providers.base import TurnResult

# --- capture: load + redact -------------------------------------------------


def test_capture_redacts_root_uuid(stub_history):
    uuid = "11111111-2222-3333-4444-555555555555"
    stub_history["transcript"] = (
        f"## USER\n<meta>START SESSION {uuid} #1</meta>\ngo\n\n"
        f"## ASSISTANT\ndone, ref {uuid}"
    )
    text = capture.capture(uuid, "fake_history", uuid)
    assert uuid not in text
    assert REDACTED_ROOT in text
    assert "done, ref" in text


@pytest.mark.parametrize("transcript", ["", "   \n  "], ids=["empty", "whitespace"])
def test_capture_returns_none_when_no_transcript(stub_history, transcript):
    stub_history["transcript"] = transcript
    assert capture.capture("root", "fake_history", "root") is None


# --- loop wiring ------------------------------------------------------------


def _runner_ok(provider, spec):
    return TurnResult(
        exit_code=0, timed_out=False, duration_s=0.1,
        native_session_id="sess-1", stdout="", stderr="",
    )


def test_loop_writes_transcript_and_names_it_for_operator(config, task, stub_history):
    slug, paths = task
    stub_history["transcript"] = (
        "## USER\ngo\n\n"
        "## ASSISTANT\nTOOL_RESULT\n> FULL UNTRUNCATED OUTPUT\n\ndid it"
    )
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"done"}'

    result = run_task(
        slug, config, FakeProvider(), _runner_ok, operator_invoker, first_prompt="go"
    )
    assert result.status is TaskStatus.COMPLETE

    # A single transcript file exists and holds the untruncated payload.
    assert paths.transcript.exists()
    assert paths.transcript.name == "transcript.md"
    assert "FULL UNTRUNCATED OUTPUT" in paths.transcript.read_text(encoding="utf-8")

    # The review prompt points the operator at that file.
    assert "transcript.md`" in prompts[-1]


def test_loop_overwrites_single_transcript_across_turns(config, task, stub_history):
    """One file, rewritten each turn -- not one file per iteration."""
    slug, paths = task
    stub_history["transcript"] = "## USER\ngo\n\n## ASSISTANT\nturn output"

    verdicts = iter([
        '{"verdict":"continue","next_prompt":"again"}',
        '{"verdict":"done"}',
    ])

    result = run_task(
        slug, config, FakeProvider(), _runner_ok, lambda p: next(verdicts), first_prompt="go"
    )
    assert result.status is TaskStatus.COMPLETE
    assert result.iterations == 2
    # Exactly one transcript artifact, at the task root -- no per-turn files.
    assert paths.transcript.exists()
    assert not (paths.root / "turns").exists()


def test_loop_survives_capture_failure(config, task, stub_history, monkeypatch):
    """Capture is best-effort: a history error must not fail the run."""
    slug, paths = task

    def boom(search, provider):
        raise RuntimeError("history unavailable")

    # Only the capture load blows up; _record_native's find_session is stubbed
    # by the stub_history fixture and stays intact.
    monkeypatch.setattr("backseat_harness.capture.history.load_full_transcript", boom)

    result = run_task(
        slug, config, FakeProvider(), _runner_ok,
        lambda p: '{"verdict":"done"}', first_prompt="go",
    )
    assert result.status is TaskStatus.COMPLETE
    assert not paths.transcript.exists()


def test_plan_phase_on_a_fresh_task_writes_no_transcript(config, task, stub_history):
    """A new task has no session, so there is nothing for the plan phase to capture.

    The plan phase does capture when resuming; on a fresh task the history lookup
    for the just-minted root UUID comes back empty, so no file is written and the
    prompt names none.
    """
    slug, paths = task
    stub_history["transcript"] = ""  # nothing stored: this root has never run
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"ask_human","notes":"which framework?"}'

    result = run_task(slug, config, FakeProvider(), _runner_ok, operator_invoker)
    assert result.status is TaskStatus.BLOCKED
    assert not paths.transcript.exists()
    assert "Full session transcript" not in prompts[0]


def test_plan_phase_on_resume_names_the_transcript(config, task, stub_history):
    """Re-planning gets the same untruncated record reviewing does.

    On a resume the operator is re-deciding the task with no turn to grade, so
    the session transcript is the only evidence available -- and the file left on
    disk by the last managed turn (or a takeover) predates whatever advanced the
    session since, so the plan phase re-captures before the call.
    """
    from backseat_harness import storage
    from backseat_harness.models import Binding, Generation, TaskState

    slug, paths = task
    storage.save_binding(
        paths,
        Binding(
            root_uuid="root-1",
            provider="fake",
            generations=[Generation(n=1, native_id="ses_old")],
        ),
    )
    storage.save_state(paths, TaskState(slug=slug, iteration=1, generation=1))
    stub_history["transcript"] = (
        "## USER\n<meta>START SESSION root-1 #1</meta>\ngo\n\n"
        "## ASSISTANT\nTOOL_RESULT\n> FULL UNTRUNCATED OUTPUT"
    )
    prompts: list[str] = []

    def operator_invoker(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict":"ask_human","notes":"which framework?"}'

    result = run_task(slug, config, FakeProvider(), _runner_ok, operator_invoker)
    assert result.status is TaskStatus.BLOCKED
    assert "FULL UNTRUNCATED OUTPUT" in paths.transcript.read_text(encoding="utf-8")
    assert "Full session transcript" in prompts[0]
    assert "transcript.md`" in prompts[0]
    # Redaction still applies: the operator must never see the raw root UUID.
    assert "root-1" not in prompts[0]

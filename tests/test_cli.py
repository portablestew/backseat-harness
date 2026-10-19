"""CLI lane creation: `run`'s second argument is the goal, or a brief."""

import json
import os

import pytest

from backseat_harness import db, spawn
from backseat_harness.cli import _ensure_goal, main
from backseat_harness.config import load_config


@pytest.fixture(autouse=True)
def _no_real_guard_waits(monkeypatch):
    """Guard-row polling waits on real time; tests take the single-check path.

    Production polls for up to _START_GRACE_S / _STOP_GRACE_S because a
    daemon booting or a tree being torn down lands asynchronously. With the
    grace at zero the same helpers check exactly once, which keeps the
    suite instant while still exercising the predicates.
    """
    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "_START_GRACE_S", 0.0)
    monkeypatch.setattr(cli, "_STOP_GRACE_S", 0.0)


def test_run_requires_goal_for_new_task(tmp_path, capsys):
    rc = main(["-C", str(tmp_path), "run", "fresh"])
    assert rc == 2
    assert "second argument" in capsys.readouterr().out
    # Nothing is created on the error path.
    assert not (tmp_path / ".backseat").exists()


def test_run_accepts_positional_goal_for_new_task(tmp_path, monkeypatch):
    """`run slug "goal"` creates goal.md; `run slug` alone does not."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli
    from backseat_harness.models import TaskResult, TaskStatus

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())

    def fake_run_task(**kwargs):
        return TaskResult(slug="fresh", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    rc = main(["-C", str(tmp_path), "run", "fresh", "Build the thing."])
    assert rc == 0
    paths = load_config(tmp_path).task_paths("fresh")
    assert paths.goal.read_text(encoding="utf-8") == "Build the thing.\n"


def test_ensure_goal_creates_lane(tmp_path):
    paths = load_config(tmp_path).task_paths("fresh")
    assert _ensure_goal(paths, "  Add GET /hello.  ") == (None, None)
    assert paths.goal.read_text(encoding="utf-8") == "Add GET /hello.\n"


def test_ensure_goal_reads_second_argument_as_a_note(tmp_path):
    """A lane with a goal turns a differing argument into a brief, not a rewrite."""
    paths = load_config(tmp_path).task_paths("t")
    _ensure_goal(paths, "Goal")
    assert _ensure_goal(paths, "D1: keep the shared budget") == (
        None,
        "D1: keep the shared budget",
    )
    # The goal is never rewritten by a brief.
    assert paths.goal.read_text(encoding="utf-8") == "Goal\n"


def test_ensure_goal_ignores_text_identical_to_the_goal(tmp_path):
    """Passing the goal back is a wrapper forwarding config, not an instruction."""
    paths = load_config(tmp_path).task_paths("t")
    _ensure_goal(paths, "Goal")
    assert _ensure_goal(paths, "Goal") == (None, None)
    assert _ensure_goal(paths, "  Goal  ") == (None, None)
    assert _ensure_goal(paths, None) == (None, None)


def test_ensure_goal_fills_an_empty_scaffold(tmp_path):
    """`init t` writes an empty goal.md; the argument is still the goal, not a brief.

    The emptiness check has to come first. Tested in the other order, a new
    lane's argument reads as a *changed* goal against the empty file, the goal
    is refused outright, and the alternative the error suggests dead-ends behind
    it.
    """
    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("\n", encoding="utf-8")
    assert _ensure_goal(paths, "The real goal") == (None, None)
    assert paths.goal.read_text(encoding="utf-8") == "The real goal\n"


def test_ensure_goal_rejects_empty_scaffold_with_no_argument(tmp_path):
    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("\n", encoding="utf-8")
    error, brief = _ensure_goal(paths, None)
    assert brief is None
    assert error and "is empty" in error


def test_takeover_unknown_task(tmp_path, capsys):
    rc = main(["-C", str(tmp_path), "takeover", "ghost"])
    assert rc == 2
    assert "No task 'ghost'" in capsys.readouterr().out


def test_takeover_opens_session_then_resumes(tmp_path, monkeypatch):
    """takeover hands off interactively with the native id, then re-plans."""
    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.config import load_config
    from backseat_harness.models import Binding, Generation
    from backseat_harness.providers import get_provider

    ws = tmp_path
    config = load_config(ws)
    paths = config.task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    # A prior run left a paused task bound to a native session.
    binding = Binding(root_uuid="root-1", provider="opencode")
    binding.generations.append(Generation(n=1, native_id="ses_live"))
    storage.save_binding(paths, binding)

    seen = {}

    def fake_run_interactive(provider, native_id, cwd):
        seen["provider"] = provider.name
        seen["native_id"] = native_id
        seen["cwd"] = str(cwd)
        return 0

    resumed = {}

    def fake_run_task(**kwargs):
        resumed["first_prompt"] = kwargs["first_prompt"]
        from backseat_harness.models import TaskResult, TaskStatus

        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli.spawn, "run_interactive", fake_run_interactive)
    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    rc = main(["-C", str(ws), "takeover", "t"])
    assert rc == 0
    assert get_provider("opencode").interactive_command(seen["native_id"]) == [
        "opencode",
        "--session",
        "ses_live",
    ]
    assert seen["native_id"] == "ses_live"
    # Resume with no first_prompt: the operator re-plans from the advanced session.
    assert resumed["first_prompt"] is None


def test_takeover_prefers_binding_provider_over_config(tmp_path, monkeypatch):
    """The binding's own provider wins over config for an existing chain.

    Resuming a native session under a different CLI fails outright (an opencode
    ``ses_`` id means nothing to ``claude --resume``), so a config switch may
    not change the CLI out from under a bound session -- not for the
    interactive handoff, and not for the resume that follows it.
    """
    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.config import load_config
    from backseat_harness.models import Binding, Generation, TaskResult, TaskStatus

    ws = tmp_path
    (ws / ".backseat").mkdir()
    (ws / ".backseat" / "config.toml").write_text(
        '[harness]\nprovider = "claude"\n', encoding="utf-8"
    )
    config = load_config(ws)
    paths = config.task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    binding = Binding(root_uuid="root-1", provider="opencode")
    binding.generations.append(Generation(n=1, native_id="ses_live"))
    storage.save_binding(paths, binding)

    seen = {}

    def fake_run_interactive(provider, native_id, cwd):
        seen["provider"] = provider.name
        seen["native_id"] = native_id
        return 0

    def fake_run_task(**kwargs):
        seen["resume_provider"] = kwargs["provider"].name
        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli.spawn, "run_interactive", fake_run_interactive)
    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    rc = main(["-C", str(ws), "takeover", "t"])
    assert rc == 0
    # Config says claude, the chain is opencode's: the binding's CLI must win.
    assert seen["provider"] == "opencode"
    assert seen["native_id"] == "ses_live"
    assert seen["resume_provider"] == "opencode"


def test_takeover_falls_back_when_binding_provider_unregistered(
    tmp_path, monkeypatch, capsys
):
    """A binding whose provider this build no longer registers cannot win:
    config decides, and the mismatch is named on stderr rather than swallowed.
    """
    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.config import load_config
    from backseat_harness.models import Binding, Generation, TaskResult, TaskStatus

    ws = tmp_path
    config = load_config(ws)
    paths = config.task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    binding = Binding(root_uuid="root-1", provider="ghost")
    binding.generations.append(Generation(n=1, native_id="ses_dead"))
    storage.save_binding(paths, binding)

    seen = {}

    def fake_run_interactive(provider, native_id, cwd):
        seen["provider"] = provider.name
        return 0

    def fake_run_task(**kwargs):
        seen["resume_provider"] = kwargs["provider"].name
        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli.spawn, "run_interactive", fake_run_interactive)
    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    rc = main(["-C", str(ws), "takeover", "t"])
    assert rc == 0
    assert seen["provider"] == "opencode"
    assert seen["resume_provider"] == "opencode"
    err = capsys.readouterr().err
    assert "binding provider 'ghost' is not registered" in err
    assert "using configured provider 'opencode'" in err


def test_takeover_no_resume_skips_loop(tmp_path, monkeypatch, capsys):
    import backseat_harness.cli as cli
    from backseat_harness.config import load_config

    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")

    monkeypatch.setattr(cli.spawn, "run_interactive", lambda provider, native_id, cwd: 0)

    def no_run(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("loop must not run with --no-resume")

    monkeypatch.setattr(cli, "run_task", no_run)
    rc = main(["-C", str(tmp_path), "takeover", "t", "--no-resume"])
    assert rc == 0
    assert "not resuming" in capsys.readouterr().out.lower()


# --- takeover stops only the target lane (3c: Q6-Q9) ------------------------
#
# Real temp registry; the pid seam and the tree kill are faked over one live
# set, so no test asks the OS about a pid or kills anything real.

LANE_A = ".backseat/tasks/a"
LANE_B = ".backseat/tasks/b"


class _Live:
    """The registry's pid seam over a fixed live set (pid -> recorded start)."""

    def __init__(self, live: dict[int, float]):
        self.live = live

    def __call__(self, pid):
        if pid not in self.live:
            raise db.psutil.NoSuchProcess(pid)
        return _Proc(self.live[pid])


class _Proc:
    def __init__(self, create_time: float):
        self._create_time = create_time

    def is_running(self) -> bool:
        return True

    def create_time(self) -> float:
        return self._create_time


def _seat(monkeypatch, live: dict[int, float], events: list[str], *, spare=frozenset()):
    """Fake the pid seam and the tree kill. A kill logs ``kill(pid)`` and drops
    the pid from ``live``; a pid in ``spare`` survives its kill."""
    monkeypatch.setattr(db, "_process", _Live(live))

    def fake_kill(pid, *, budget_s=0, protect=frozenset()):
        if pid in protect:
            return
        events.append(f"kill({pid})")
        if pid not in spare:
            live.pop(pid, None)

    monkeypatch.setattr(spawn, "kill_pid_tree", fake_kill)


def test_takeover_stops_only_the_target_lane(tmp_path, monkeypatch):
    """Q6: lane A's harness and providers die; lane B and the daemon are untouched."""
    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    assert main(["-C", str(tmp_path), "init", "b", "Goal"]) == 0
    with db.open_db() as conn:
        db.claim_task(conn, tmp_path, LANE_A, 101, 1000.0)
        db.record_provider(conn, tmp_path, LANE_A, [201, 202], [2001.0, 2002.0])
        db.claim_task(conn, tmp_path, LANE_B, 301, 3000.0)
        db.record_provider(conn, tmp_path, LANE_B, [401], [4001.0])
        db.claim_daemon(conn, 900, 9000.0)
        before_b = db.get_task(conn, tmp_path, LANE_B)

    live = {101: 1000.0, 201: 2001.0, 202: 2002.0, 301: 3000.0, 401: 4001.0, 900: 9000.0}
    live[os.getpid()] = 5000.0  # the takeover's own claim stamps this pid
    events: list[str] = []
    _seat(monkeypatch, live, events)
    monkeypatch.setattr(spawn, "run_interactive", lambda provider, native_id, cwd: 0)

    assert main(["-C", str(tmp_path), "takeover", "a", "--no-resume"]) == 0
    assert events == ["kill(101)", "kill(201)", "kill(202)"]
    assert {301, 401, 900} <= set(live)
    with db.open_db() as conn:
        assert db.get_task(conn, tmp_path, LANE_B) == before_b
        assert db.get_task(conn, tmp_path, LANE_A) is None


def test_takeover_never_kills_a_none_start_provider(tmp_path, monkeypatch):
    """Q4(b): a provider recorded with no start (legacy row or failed create_time)
    is never signalled, yet the harness still stops and the takeover proceeds."""
    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    with db.open_db() as conn:
        db.claim_task(conn, tmp_path, LANE_A, 101, 1000.0)
        db.record_provider(conn, tmp_path, LANE_A, [201, 202], [2001.0, None])

    # 202 is live, so existence alone would pass: only the None guard spares it.
    live = {101: 1000.0, 201: 2001.0, 202: 2002.0, os.getpid(): 5000.0}
    events: list[str] = []
    _seat(monkeypatch, live, events)

    def fake_run_interactive(provider, native_id, cwd):
        events.append("run_interactive")
        return 0

    monkeypatch.setattr(spawn, "run_interactive", fake_run_interactive)
    assert main(["-C", str(tmp_path), "takeover", "a", "--no-resume"]) == 0
    assert events == ["kill(101)", "kill(201)", "run_interactive"]
    assert 202 in live
    with db.open_db() as conn:
        assert db.get_task(conn, tmp_path, LANE_A) is None


def test_takeover_waits_for_the_lane_dead_before_the_handover(tmp_path, monkeypatch):
    """Q7: kill(harness), kill(providers), then the claim and the handover."""
    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    with db.open_db() as conn:
        db.claim_task(conn, tmp_path, LANE_A, 101, 1000.0)
        db.record_provider(conn, tmp_path, LANE_A, [201, 202], [2001.0, 2002.0])

    live = {101: 1000.0, 201: 2001.0, 202: 2002.0, os.getpid(): 5000.0}
    events: list[str] = []
    _seat(monkeypatch, live, events)

    def fake_run_interactive(provider, native_id, cwd):
        events.append("run_interactive")
        with db.open_db() as conn:
            row = db.get_task(conn, tmp_path, LANE_A)
        # The claim is ours by the time the terminal is handed over.
        assert row["harness_pid"] == os.getpid()
        return 0

    monkeypatch.setattr(spawn, "run_interactive", fake_run_interactive)
    assert main(["-C", str(tmp_path), "takeover", "a", "--no-resume"]) == 0
    assert events == ["kill(101)", "kill(201)", "kill(202)", "run_interactive"]


def test_takeover_refuses_when_the_harness_survives_the_grace(
    tmp_path, monkeypatch, capsys
):
    """Q7 variant: a harness that outlives its kill refuses, and nothing is handed over."""
    import backseat_harness.cli as cli

    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    with db.open_db() as conn:
        db.claim_task(conn, tmp_path, LANE_A, 101, 1000.0)
        db.record_provider(conn, tmp_path, LANE_A, [201], [2001.0])

    live = {101: 1000.0, 201: 2001.0, os.getpid(): 5000.0}
    events: list[str] = []
    _seat(monkeypatch, live, events, spare={101})
    monkeypatch.setattr(cli, "_KILL_GRACE_S", 0.0)

    def no_handover(provider, native_id, cwd):  # pragma: no cover - must not run
        raise AssertionError("terminal must not be handed over while the harness lives")

    monkeypatch.setattr(spawn, "run_interactive", no_handover)
    assert main(["-C", str(tmp_path), "takeover", "a"]) == 2
    assert "lane a still running after 0s; not taking over" in capsys.readouterr().out
    assert events == ["kill(101)", "kill(201)"]
    # No claim was taken, so the row still names the harness that survived.
    with db.open_db() as conn:
        assert db.get_task(conn, tmp_path, LANE_A)["harness_pid"] == 101


@pytest.mark.parametrize(
    "case",
    ["resumed", "no-resume", "interactive-exit", "interactive-raises", "resume-interrupted"],
)
def test_takeover_releases_the_claim_on_every_path(tmp_path, monkeypatch, case):
    """Q9: no row survives any exit; a resumed run sees the recorder installed."""
    import backseat_harness.cli as cli
    from backseat_harness.models import TaskResult, TaskStatus

    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    _seat(monkeypatch, {os.getpid(): 5000.0}, [])
    seen: dict = {}

    def fake_run_interactive(provider, native_id, cwd):
        if case == "interactive-raises":
            raise RuntimeError("boom")
        return 3 if case == "interactive-exit" else 0

    def fake_run_task(**kwargs):
        if case not in {"resumed", "resume-interrupted"}:
            raise AssertionError("the resume must not run")
        if case == "resume-interrupted":
            raise KeyboardInterrupt
        spawn._provider_recorder.get()([(777, 7770.0)])
        with db.open_db() as conn:
            row = db.get_task(conn, tmp_path, LANE_A)
            seen["providers"] = [pid for pid, _ in db.provider_entries(row)]
        return TaskResult(slug="a", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(spawn, "run_interactive", fake_run_interactive)
    monkeypatch.setattr(cli, "run_task", fake_run_task)
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    argv = ["-C", str(tmp_path), "takeover", "a"]
    if case == "no-resume":
        argv.append("--no-resume")
    if case == "interactive-raises":
        with pytest.raises(RuntimeError, match="boom"):
            main(argv)
    elif case == "resume-interrupted":
        with pytest.raises(KeyboardInterrupt):
            main(argv)
    else:
        rc = main(argv)
        assert rc == {"resumed": 0, "no-resume": 0, "interactive-exit": 3}[case]
    if case == "resumed":
        assert seen["providers"] == [777]
    with db.open_db() as conn:
        assert db.get_task(conn, tmp_path, LANE_A) is None


def test_resolve_argv_uses_path_lookup(monkeypatch):
    """subprocess can't find npm .cmd shims on Windows; which() can."""
    import pytest

    from backseat_harness import spawn
    from backseat_harness.providers.base import ProviderError

    monkeypatch.setattr(spawn.shutil, "which", lambda name: rf"C:\bin\{name}.cmd")
    assert spawn.resolve_argv(["opencode", "run"]) == [r"C:\bin\opencode.cmd", "run"]

    monkeypatch.setattr(spawn.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="not found on PATH"):
        spawn.resolve_argv(["opencode"])


def test_result_line_omits_reason_on_complete_only():
    from backseat_harness.cli import _result_line
    from backseat_harness.models import TaskResult, TaskStatus

    complete = TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=4, reason="goal met")
    assert _result_line(complete) == "t: complete after 4 iterations"
    blocked = TaskResult(slug="t", status=TaskStatus.BLOCKED, iterations=2, reason="which db?")
    assert _result_line(blocked) == "t: blocked after 2 iterations (which db?)"
    out_of_budget = TaskResult(
        slug="t",
        status=TaskStatus.CONTINUE,
        iterations=47,
        reason="max_iterations reached after 30 turns this run (47 total)",
    )
    # The lane total and the run budget disagree by design, so the line has to
    # carry both rather than read as a count that overshot its own limit.
    assert _result_line(out_of_budget) == (
        "t: continue after 47 iterations "
        "(max_iterations reached after 30 turns this run (47 total))"
    )


def _blocked_lane(tmp_path, slug="t", reason="D1: keep the shared budget, or split it?"):
    """A lane left blocked by an ask_human, with its question recorded."""
    from backseat_harness import storage
    from backseat_harness.models import TaskState, TaskStatus

    paths = load_config(tmp_path).task_paths(slug)
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    storage.save_state(
        paths, TaskState(slug=slug, status=TaskStatus.BLOCKED, iteration=45, reason=reason)
    )
    return paths


def test_bare_resume_refuses_a_blocked_lane_and_quotes_the_question(tmp_path, monkeypatch, capsys):
    """A bare `run` on a blocked lane must not silently drop the question."""
    import backseat_harness.cli as cli

    def no_run(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a blocked lane must not resume without a brief")

    monkeypatch.setattr(cli, "run_task", no_run)
    _blocked_lane(tmp_path, reason="D1: keep the shared budget, or split it?")

    rc = main(["-C", str(tmp_path), "run", "t"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "blocked, not continue" in out
    # The question is quoted whole, not truncated the way progress truncates it.
    assert "D1: keep the shared budget, or split it?" in out
    assert 'run t "' in out


def test_bare_resume_refuses_a_complete_lane(tmp_path, monkeypatch, capsys):
    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.models import TaskState, TaskStatus

    def no_run(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a complete lane must not resume without a brief")

    monkeypatch.setattr(cli, "run_task", no_run)
    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    storage.save_state(paths, TaskState(slug="t", status=TaskStatus.COMPLETE, iteration=9))

    rc = main(["-C", str(tmp_path), "run", "t"])
    assert rc == 2
    assert "complete, not continue" in capsys.readouterr().out


def test_bare_resume_allowed_on_a_continue_lane(tmp_path, monkeypatch):
    from conftest import FakeProvider

    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.models import TaskState, TaskStatus

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    seen = {}

    def fake_run_task(**kwargs):
        seen.update(kwargs)
        from backseat_harness.models import TaskResult

        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")
    storage.save_state(paths, TaskState(slug="t", status=TaskStatus.CONTINUE, iteration=3))

    rc = main(["-C", str(tmp_path), "run", "t"])
    assert rc == 0
    assert seen["owner_brief"] is None


def test_note_unblocks_a_blocked_lane(tmp_path, monkeypatch):
    """`run t "the answer"` is the unblock: it resumes and carries the brief."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    seen = {}

    def fake_run_task(**kwargs):
        seen.update(kwargs)
        from backseat_harness.models import TaskResult, TaskStatus

        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    _blocked_lane(tmp_path)

    rc = main(["-C", str(tmp_path), "run", "t", "Keep the shared budget; D2 report-only."])
    assert rc == 0
    assert seen["owner_brief"] == "Keep the shared budget; D2 report-only."


def test_note_from_a_continue_lane_is_allowed(tmp_path, monkeypatch):
    """No blocked_on gate: the owner may add commentary mid-flight."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    seen = {}

    def fake_run_task(**kwargs):
        seen.update(kwargs)
        from backseat_harness.models import TaskResult, TaskStatus

        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.goal.write_text("Goal\n", encoding="utf-8")

    assert main(["-C", str(tmp_path), "run", "t", "also sweep the docs"]) == 0
    assert seen["owner_brief"] == "also sweep the docs"


def test_note_with_prompt_is_rejected(tmp_path, monkeypatch, capsys):
    """-p skips the operator, so a brief would have nowhere to go."""
    import backseat_harness.cli as cli

    def no_run(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a brief paired with --prompt must be rejected")

    monkeypatch.setattr(cli, "run_task", no_run)
    _blocked_lane(tmp_path)

    rc = main(["-C", str(tmp_path), "run", "t", "the answer", "-p", "do the thing"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "brief needs the operator" in out
    assert "--prompt" in out


def test_prompt_without_a_note_still_resumes_a_blocked_lane(tmp_path, monkeypatch):
    """-p bypasses the gate: it is the explicit "skip the operator" override.

    The gate is on the second argument, which asks the operator to act. -p does
    not involve the operator at all, so refusing it would remove the one way to
    drive a blocked lane without the operator in the loop.
    """
    from conftest import FakeProvider

    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    seen = {}

    def fake_run_task(**kwargs):
        seen.update(kwargs)
        from backseat_harness.models import TaskResult, TaskStatus

        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    _blocked_lane(tmp_path)

    rc = main(["-C", str(tmp_path), "run", "t", "-p", "do the thing"])
    assert rc == 0
    assert seen["first_prompt"] == "do the thing"
    assert seen["owner_brief"] is None


def _capture_operator_spawn(monkeypatch, provider=None, call=None):
    """Stub the operator spawn end to end and capture the stdin prompt."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "get_provider", lambda name: provider or FakeProvider())
    monkeypatch.setattr(cli.spawn, "resolve_argv", lambda cmd: cmd)
    seen = {}

    def fake_run_with_timeout(cmd, **kwargs):
        seen["input"] = kwargs["input"]
        seen["argv"] = list(cmd)
        seen["timeout_s"] = kwargs["timeout_s"]
        return call() if call is not None else spawn.CompletedCall(0, "ok", "", False)

    monkeypatch.setattr(cli.spawn, "run_with_timeout", fake_run_with_timeout)
    return seen


def test_operator_invoker_prepends_persona(config, monkeypatch):
    """Every provider gets the persona as a prompt prefix, whatever its launch."""
    import backseat_harness.cli as cli
    from backseat_harness.operator import load_persona

    seen = _capture_operator_spawn(monkeypatch)
    cli._make_operator_invoker(config, config.operator)("Do the review.")
    assert seen["input"] == f"{load_persona()}\n\nDo the review."


def test_operator_invoker_prepends_persona_for_named_agent(config, monkeypatch):
    """[operator].agent changes the agent, not the persona delivery."""
    from dataclasses import replace

    import backseat_harness.cli as cli
    from backseat_harness.operator import load_persona

    seen = _capture_operator_spawn(monkeypatch)
    op_config = replace(config.operator, agent="my-operator")
    cli._make_operator_invoker(config, op_config)("Do the review.")
    assert seen["input"] == f"{load_persona()}\n\nDo the review."


def test_operator_invoker_passes_the_configured_ceiling(config, monkeypatch):
    """[operator].timeout_s is what bounds the call: cli hands that number to
    run_with_timeout, not some hardcoded default."""
    from dataclasses import replace

    import backseat_harness.cli as cli

    seen = _capture_operator_spawn(monkeypatch)
    cli._make_operator_invoker(config, replace(config.operator, timeout_s=42))("Do the review.")
    assert seen["timeout_s"] == 42


def test_operator_invoker_flags_only_a_killed_call(config, monkeypatch):
    """The reply carries timed_out exactly when the spawn was killed -- that
    flag, not the emptiness of the text, is what the loop conditions its kill
    claim on."""
    import backseat_harness.cli as cli

    _capture_operator_spawn(monkeypatch, call=lambda: spawn.CompletedCall(0, "", "", True))
    killed = cli._make_operator_invoker(config, config.operator)("Do the review.")
    assert killed == ""
    assert getattr(killed, "timed_out", False) is True

    _capture_operator_spawn(monkeypatch, call=lambda: spawn.CompletedCall(0, "ok", "", False))
    done = cli._make_operator_invoker(config, config.operator)("Do the review.")
    assert done == "ok"
    assert getattr(done, "timed_out", False) is False


def test_both_agent_settings_reach_the_provider_argv(config, monkeypatch):
    """[harness].agent (managed) and [operator].agent both land as --agent."""
    import pathlib as _pathlib
    from dataclasses import replace

    import backseat_harness.cli as cli
    from backseat_harness.providers.opencode import OpencodeProvider
    from backseat_harness.spawn import SpawnSpec

    # Managed side: loop.py composes ("--agent", config.agent) into SpawnSpec.
    spec = SpawnSpec(prompt="p", cwd=_pathlib.Path("."), extra_args=("--agent", "my-builder"))
    argv = OpencodeProvider().build_command(spec)
    assert argv[-2:] == ["--agent", "my-builder"]

    # Operator side: cli.py composes it from op_config.agent, with the persona
    # prepended, against a real provider rather than the argv-dropping fake.
    real_get = cli.get_provider
    monkeypatch.setattr(cli, "get_provider", lambda name: OpencodeProvider())
    seen = {}
    monkeypatch.setattr(cli.spawn, "resolve_argv", lambda cmd: cmd)
    monkeypatch.setattr(
        cli.spawn,
        "run_with_timeout",
        lambda cmd, **kwargs: (
            seen.update(argv=list(cmd)) or spawn.CompletedCall(0, "ok", "", False)
        ),
    )
    try:
        op_config = replace(config.operator, agent="my-reviewer")
        cli._make_operator_invoker(config, op_config)("Do the review.")
    finally:
        monkeypatch.setattr(cli, "get_provider", real_get)
    assert seen["argv"][-2:] == ["--agent", "my-reviewer"]


def test_run_sees_task_layer(tmp_path, monkeypatch):
    """`run` resolves provider from the task-level config.toml, not the workspace's."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli
    from backseat_harness.models import TaskResult, TaskStatus

    ws = tmp_path
    (ws / ".backseat").mkdir()
    (ws / ".backseat" / "config.toml").write_text(
        '[harness]\nprovider = "opencode"\n', encoding="utf-8"
    )
    task = ws / ".backseat" / "tasks" / "t"
    task.mkdir(parents=True)
    (task / "config.toml").write_text('[harness]\nprovider = "claude"\n', encoding="utf-8")

    seen = {}
    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    def fake_run_task(**kwargs):
        seen["config"] = kwargs["config"]
        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    rc = main(["-C", str(ws), "run", "t", "Goal"])
    assert rc == 0
    assert seen["config"].provider == "claude"


def test_run_prefers_binding_provider_over_config(tmp_path, monkeypatch):
    """`run` resumes an existing chain under the binding's own provider.

    The task-layer config may say claude while the chain was born on opencode;
    the binding is what knows which CLI can resume that session, so it wins
    for the managed side (config still resolves -- and is reported -- as
    configured).
    """
    import backseat_harness.cli as cli
    from backseat_harness import storage
    from backseat_harness.config import load_config
    from backseat_harness.models import Binding, TaskResult, TaskStatus

    ws = tmp_path
    (ws / ".backseat").mkdir()
    (ws / ".backseat" / "config.toml").write_text(
        '[harness]\nprovider = "claude"\n', encoding="utf-8"
    )
    config = load_config(ws)
    paths = config.task_paths("t")
    paths.root.mkdir(parents=True)
    storage.save_binding(paths, Binding(root_uuid="root-1", provider="opencode"))

    seen = {}
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")

    def fake_run_task(**kwargs):
        seen["provider"] = kwargs["provider"].name
        seen["config_provider"] = kwargs["config"].provider
        return TaskResult(slug="t", status=TaskStatus.COMPLETE, iterations=1)

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    rc = main(["-C", str(ws), "run", "t", "Goal"])
    assert rc == 0
    assert seen["config_provider"] == "claude"
    assert seen["provider"] == "opencode"


def test_cli_call_sites_pass_slug(tmp_path, monkeypatch, capsys):
    """Every command but `list` threads its slug into `load_config`."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli
    from backseat_harness import config as config_mod
    from backseat_harness.models import TaskResult, TaskStatus

    calls = []
    real = config_mod.load_config

    def spy(workspace, slug=None, **kwargs):
        calls.append((str(workspace), slug))
        return real(workspace, slug, **kwargs)

    monkeypatch.setattr(cli, "load_config", spy)
    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: lambda p: "")
    monkeypatch.setattr(
        cli,
        "run_task",
        lambda **kwargs: TaskResult(slug="u", status=TaskStatus.COMPLETE, iterations=1),
    )

    ws = str(tmp_path)
    main(["-C", ws, "init", "t"])
    main(["-C", ws, "run", "u", "Goal"])
    main(["-C", ws, "takeover", "ghost"])
    main(["-C", ws, "status", "ghost"])
    main(["-C", ws, "list"])
    capsys.readouterr()

    assert calls == [(ws, "t"), (ws, "u"), (ws, "ghost"), (ws, "ghost"), (ws, None)]


def test_list_ignores_task_config(tmp_path, capsys):
    """`list` never parses a task config: a malformed one cannot break it."""
    task = tmp_path / ".backseat" / "tasks" / "t"
    task.mkdir(parents=True)
    (task / "config.toml").write_text("this is not valid toml = = =\n", encoding="utf-8")

    rc = main(["-C", str(tmp_path), "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "t" in out


# --- --debug ------------------------------------------------------------


def test_debug_gates_the_step_channel_only(tmp_path):
    """The flag is about what the launching terminal shows, never about whether
    the logs exist: provider call logs are written either way, being the durable
    record rather than a diagnostic aid.
    """
    import backseat_harness.progress as progress_mod

    assert progress_mod.debug_enabled() is False

    main(["-C", str(tmp_path), "--debug", "list"])
    assert progress_mod.debug_enabled() is True

    main(["-C", str(tmp_path), "list"])
    assert progress_mod.debug_enabled() is False


def test_a_debug_run_announces_and_times_its_steps(tmp_path, capsys, monkeypatch):
    """The elapsed figure is what makes the channel readable after the fact: a
    run that stalled shows one step with a big number, not a uniform gap.

    Driven through the real loop so the steps under test are the ones a run
    actually reaches -- the history read at startup being the first, and the one
    that most often explains a stalled start.
    """
    from conftest import FakeProvider

    import backseat_harness.cli as cli
    from backseat_harness.loop import run_task as real_run_task
    from backseat_harness.providers.base import TurnResult

    monkeypatch.setattr(cli, "get_provider", lambda name: FakeProvider())

    def cli_loop(**kwargs):
        """The real loop, with the two spawns faked."""
        kwargs["managed_runner"] = lambda prov, spec: TurnResult(
            exit_code=0,
            timed_out=False,
            duration_s=0.1,
            native_session_id="sess-1",
            stdout="",
            stderr="",
        )
        kwargs["operator_invoker"] = lambda p: '{"verdict":"done"}'
        return real_run_task(**kwargs)

    monkeypatch.setattr(cli, "run_task", cli_loop)
    monkeypatch.setattr("backseat_harness.history.find_session", lambda search, provider: None)
    monkeypatch.setattr("backseat_harness.history.fork_session", lambda search, provider: "")
    monkeypatch.setattr(
        "backseat_harness.history.load_full_transcript", lambda search, provider: ""
    )

    main(["-C", str(tmp_path), "init", "d", "Do it."])
    capsys.readouterr()
    rc = main(["-C", str(tmp_path), "--debug", "run", "d"])

    assert rc == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("[dbg]")]

    assert lines, "a debug run says something even before its first slow step"
    # The run names itself before it starts reading anything: without that first
    # line, a run stalled in a chat-history lookup looks like one that never
    # started.
    assert "starting" in lines[0]
    # Each step announces, then reports how long it took -- the elapsed figure is
    # what makes a stalled run readable afterwards as one large number rather
    # than a uniform gap.
    assert any(line.startswith("[dbg] ->") for line in lines)
    assert any(line.startswith("[dbg] <-") and "s)" in line for line in lines)
    # The provider call is an expected silence bounded by a configured ceiling,
    # so it announces its ceiling and where its output is going.
    assert any("calling operator" in line and "1800s ceiling" in line for line in lines)


def test_bare_daemon_prints_help_and_exits_2(capsys):
    rc = main(["daemon"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "usage" in out.lower() or "daemon" in out


@pytest.mark.parametrize(
    "argv, expect",
    [
        (["daemon", "run"], {"daemon_command": "run", "func": "_cmd_daemon"}),
        (["--format", "json", "daemon", "status"], {"format": "json", "daemon_command": "status"}),
        (["--format", "json", "daemon", "stop"], {"format": "json", "daemon_command": "stop"}),
        (
            ["--format", "json", "daemon", "restart"],
            {"format": "json", "daemon_command": "restart"},
        ),
        (
            ["--format", "json", "daemon", "install"],
            {"format": "json", "daemon_command": "install"},
        ),
        (
            ["daemon", "install", "--uninstall", "--path-override", "/tmp/custom"],
            {
                "daemon_command": "install",
                "uninstall": True,
                "path_override": "/tmp/custom",
                "func": "_cmd_daemon_install",
            },
        ),
    ],
    ids=["run", "status", "stop", "restart", "install", "install-uninstall-path"],
)
def test_daemon_subcommands_parse_to_their_handlers(argv, expect):
    import backseat_harness.cli as cli

    args = cli._build_parser().parse_args(argv)
    assert args.command == "daemon"
    for key, value in expect.items():
        if key == "func":
            assert args.func == getattr(cli, value)
        else:
            assert getattr(args, key) == value


@pytest.mark.parametrize(
    "record, live, registered, installed, expected",
    [
        (
            {"pid": 1234, "start": 9999.0},
            True,
            True,
            True,
            {"running": True, "installed": True, "pid": 1234, "start": 9999.0, "owns": True},
        ),
        (None, False, False, False, {"running": False, "installed": False}),
        (None, False, True, True, {"running": False, "installed": True}),
        (
            {"pid": 777, "start": 3000.0},
            True,
            False,
            False,
            {"running": True, "installed": False, "pid": 777, "start": 3000.0, "owns": False},
        ),
    ],
    ids=["running-registered", "not-installed", "installed-not-running", "unmanaged-live"],
)
def test_daemon_status_reports_each_state(
    monkeypatch, capsys, record, live, registered, installed, expected
):
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: record)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: live)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: registered)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: installed})(),
    )
    rc = main(["--format", "json", "daemon", "status"])
    payload = json.loads(capsys.readouterr().out)
    uptime = payload.pop("uptime", None)
    assert rc == 0
    assert payload == expected
    if expected["running"]:
        assert isinstance(uptime, (int, float))
    else:
        assert uptime is None


def test_daemon_stop_confirms_against_the_guard_row(monkeypatch, capsys):
    """A /end that ends nothing must not be reported as a stop.

    `schtasks /end` returns success even when the task has no running
    instance, and it cannot end an instance that is not the daemon's parent
    -- which is what a uv-managed venv makes of every launch, the action's
    python.exe being a launcher for the interpreter that runs the daemon.
    """
    import backseat_harness.cli as cli
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "stop", lambda: None)
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 4242, "start": 7.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    # The fallback kill is real code; never let it reach a live pid from a test.
    killed = []
    monkeypatch.setattr(cli.spawn, "kill_pid_tree", lambda pid, budget_s=0: killed.append(pid))

    rc = main(["daemon", "stop"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stop failed" in out
    assert "4242" in out
    assert "daemon stopped" not in out, "a surviving daemon must not be reported stopped"
    assert killed == [4242], "the recorded daemon is the only handle /end does not have"


def test_daemon_stop_kills_recorded_daemon_when_end_reaches_nothing(monkeypatch, capsys):
    """When /end ends no instance, stop ends the recorded process itself.

    The guard row names the interpreter that runs the daemon; killing it is
    what actually stops the daemon, and the row going quiet is the proof.
    """
    import backseat_harness.cli as cli
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "stop", lambda: None)
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 4242, "start": 7.0})
    live = [True]

    def pid_live(pid, start):
        return live[0]

    monkeypatch.setattr(db_mod, "pid_live", pid_live)

    def fake_kill(pid, budget_s=0):
        live[0] = False

    monkeypatch.setattr(cli.spawn, "kill_pid_tree", fake_kill)

    rc = main(["--format", "json", "daemon", "stop"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["stopped"] is True
    assert payload["pid"] == 4242


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"pid": 4242, "start": 7.0}, {"pid": 4242}),
        (None, {"was_running": False}),  # no record: nothing was known to be running
    ],
    ids=["recorded-daemon", "no-record"],
)
def test_daemon_stop_reports_success_when_the_daemon_goes_quiet(
    monkeypatch, capsys, record, expected
):
    """The live daemon before stop is dead after it: that is a real stop."""
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: record)

    live = [True]

    def pid_live(pid, start):
        return live[0]

    monkeypatch.setattr(db_mod, "pid_live", pid_live)
    # The stop tears the tree down: the row's owner stops being live.
    monkeypatch.setattr(platform_mod, "stop", lambda: live.__setitem__(0, False))

    rc = main(["--format", "json", "daemon", "stop"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["stopped"] is True
    assert {key: payload[key] for key in expected} == expected


def test_daemon_stop_unmanaged_live(monkeypatch, capsys):
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 9999, "start": 5000.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: False)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: False})(),
    )
    called = [False]

    def spy_stop():
        called[0] = True

    monkeypatch.setattr(platform_mod, "stop", spy_stop)
    rc = main(["daemon", "stop"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not called[0], "platform.stop must not be called for unmanaged daemon"
    assert "unmanaged" in out.lower() or "ctrl-c" in out.lower()


def test_daemon_install_registers_and_starts(monkeypatch, capsys, tmp_path):
    import backseat_harness.platform as platform_mod

    registered = [False]
    started = [False]

    def spy_register():
        registered[0] = True

    def spy_start():
        started[0] = True

    monkeypatch.setattr(platform_mod, "register", spy_register)
    monkeypatch.setattr(platform_mod, "start", spy_start)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    monkeypatch.setattr(platform_mod, "uninstall", lambda: None)
    import backseat_harness.db as db_mod

    # A start only counts once a daemon has claimed the guard, so the record
    # appears with the start rather than alongside it.
    def record(conn):
        return {"pid": 31337, "start": 1.0} if started[0] else None

    monkeypatch.setattr(db_mod, "_daemon_record", record)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    assert registered[0] is True
    assert started[0] is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload.get("warn") is None


def test_daemon_install_foreign_live_daemon_warns(monkeypatch, capsys):
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 42, "start": 1.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: False)
    monkeypatch.setattr(platform_mod, "register", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: False)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["installed"] is False
    assert payload.get("warn") is not None
    assert "live" in payload.get("warn", "").lower()
    assert "install does not kill" in payload.get("warn", "").lower()


def test_daemon_install_start_failure_not_hidden(monkeypatch, capsys):
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "register", lambda: None)

    def fail_start():
        raise RuntimeError("start failed")

    monkeypatch.setattr(platform_mod, "start", fail_start)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["installed"] is False
    assert payload.get("start_ok") is False
    assert payload.get("start_error") == "start failed"


def test_daemon_restart_reports_failure_when_daemon_survives(monkeypatch, capsys):
    """A restart that leaves the same daemon live has restarted nothing.

    /run returns as soon as the scheduler accepts the request, so the fresh
    instance can die refusing a guard it cannot take while the command is
    still told it succeeded. The reported claim comes from a new pid owning
    the guard row.
    """
    import backseat_harness.cli as cli
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "restart", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 4242, "start": 7.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    # The fallback kill is real code; never let it reach a live pid from a test.
    killed = []
    monkeypatch.setattr(cli.spawn, "kill_pid_tree", lambda pid, budget_s=0: killed.append(pid))

    rc = main(["daemon", "restart"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "restart failed" in out
    assert "4242" in out
    assert "daemon restarted" not in out
    assert killed == [4242], "the surviving daemon must be ended before a new one can start"


def test_daemon_restart_ends_old_daemon_then_starts_a_new_one(monkeypatch, capsys):
    """The uv-launcher shape: /end reaches no daemon, so restart ends the
    recorded pid, asks the task to run again, and reports the new owner."""
    import backseat_harness.cli as cli
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "restart", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    # The guard row names whoever is live; the kill is what changes that.
    live = {4242: 7.0}
    killed = []

    def record(conn):
        pid, start = next(iter(live.items()))
        return {"pid": pid, "start": start}

    def pid_live(pid, start):
        return live.get(pid) == start

    def fake_kill(pid, budget_s=0):
        killed.append(pid)
        live.pop(pid, None)
        live[5150] = 9.0

    monkeypatch.setattr(db_mod, "_daemon_record", record)
    monkeypatch.setattr(db_mod, "pid_live", pid_live)
    monkeypatch.setattr(cli.spawn, "kill_pid_tree", fake_kill)

    rc = main(["--format", "json", "daemon", "restart"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert killed == [4242]
    assert payload["restarted"] is True
    assert payload["pid"] == 5150


def test_daemon_restart_reports_new_guard_owner(monkeypatch, capsys):
    """A genuinely new daemon (different pid owning the guard) is reported."""
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)

    records = [{"pid": 4242, "start": 7.0}, {"pid": 5150, "start": 8.0}]
    reads = [0]

    def record(conn):
        index = min(reads[0], len(records) - 1)
        reads[0] += 1
        return records[index]

    monkeypatch.setattr(db_mod, "_daemon_record", record)
    monkeypatch.setattr(platform_mod, "restart", lambda: None)

    rc = main(["--format", "json", "daemon", "restart"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["restarted"] is True
    assert payload["pid"] == 5150


def test_daemon_restart_without_install_exits_2(monkeypatch, capsys):
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: False)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: False)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    rc = main(["--format", "json", "daemon", "restart"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "nothing installed" in out.lower()


def test_daemon_uninstall_removes(monkeypatch, capsys):
    import backseat_harness.platform as platform_mod

    removed = [False]

    def spy_uninstall():
        removed[0] = True

    monkeypatch.setattr(platform_mod, "uninstall", spy_uninstall)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "register", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    rc = main(["--format", "json", "daemon", "install", "--uninstall"])
    assert rc == 0
    assert removed[0] is True
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["uninstalled"] is True


def test_daemon_install_path_override_refreshed(monkeypatch, capsys):
    import os

    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "register", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    original = os.environ.get("PATH")
    rc = main(["--format", "json", "daemon", "install", "--path-override", "/fake/custom"])
    assert rc == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload.get("path_override") == "/fake/custom"
    assert os.environ.get("PATH") == original


def test_daemon_install_reports_linger_without_mutating(monkeypatch, capsys):
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "register", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "Linger=no\n", "stderr": ""})()

    monkeypatch.setattr(sp_mod, "run", fake_run)
    # Each read hands back a fresh owner, so the start check sees a new daemon
    # claim the guard after the (stubbed) start rather than the pre-install one.
    counter = {"pid": 4242}

    def record(conn):
        counter["pid"] += 1
        return {"pid": counter["pid"], "start": 9.0}

    monkeypatch.setattr(db_mod, "_daemon_record", record)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("start_ok") is True


def test_daemon_install_foreign_live_with_stale_artifact_warns(monkeypatch, capsys):
    """When a live daemon exists at install time, ownership is undeterminable
    (guard row has no owner link); warn rather than claim installed=True."""
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 99, "start": 500.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "register", lambda: None)
    monkeypatch.setattr(platform_mod, "start", lambda: None)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["installed"] is False
    assert payload.get("warn") is not None
    assert "live" in payload.get("warn", "").lower()
    assert "ownership undeterminable" in payload.get("warn", "").lower()
    assert "stale" in payload.get("warn", "").lower()


def test_daemon_install_idempotent(monkeypatch, capsys):
    """Install must be idempotent: running it twice with the same args succeeds
    both times with identical payloads (overwrites with /F on Windows, systemctl
    reload on Linux, launchctl bootstrap on macOS)."""
    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    register_calls = [0]
    start_calls = [0]

    def spy_register():
        register_calls[0] += 1

    def spy_start():
        start_calls[0] += 1

    monkeypatch.setattr(platform_mod, "register", spy_register)
    monkeypatch.setattr(platform_mod, "start", spy_start)
    # This test is about register/start being repeatable, not about the start
    # check (covered by the guard-owner tests below): the stub reports a live
    # owner so each install's start is treated as real.
    import backseat_harness.cli as cli

    monkeypatch.setattr(
        cli, "_wait_new_guard_owner", lambda pid, start, grace: {"pid": 7001, "start": 5.0}
    )
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)

    def _fake_path_artifact():
        return __import__("types").SimpleNamespace(exists=lambda s: True)()

    monkeypatch.setattr(platform_mod, "artifact_path", _fake_path_artifact)

    # First install
    rc1 = main(["--format", "json", "daemon", "install"])
    assert rc1 == 0
    payload1 = __import__("json").loads(capsys.readouterr().out)
    assert payload1["installed"] is True

    # Second install (idempotent)
    rc2 = main(["--format", "json", "daemon", "install"])
    assert rc2 == 0
    payload2 = __import__("json").loads(capsys.readouterr().out)
    assert payload2["installed"] is True

    # Both register and start should have been called twice
    assert register_calls[0] == 2
    assert start_calls[0] == 2

    # Payloads should be equivalent (same keys/values)
    assert payload1["installed"] == payload2["installed"]
    assert payload1.get("warn") == payload2.get("warn")


# --- web --mode (3e): standalone web, direct or behind caddy ---------------


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_web_caddyfile(site_port: int, web_port: int) -> None:
    from backseat_harness.web import caddy as caddy_mod

    path = caddy_mod.caddyfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"site.lan:{site_port} {{\n\treverse_proxy 127.0.0.1:{web_port}\n}}\n",
        encoding="utf-8",
    )


def _fake_uvicorn(monkeypatch, events: list[str]):
    """Replace uvicorn.Server with one that serves until should_exit; returns a started flag."""
    import threading
    import time

    started = threading.Event()

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False

        def run(self):
            events.append("uvicorn-start")
            started.set()
            deadline = time.monotonic() + 20.0
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.005)
            events.append("uvicorn-stop")

    monkeypatch.setattr("uvicorn.Server", FakeServer)
    return started


def _interrupt_main_when(predicate) -> None:
    """Send Ctrl-C to the main thread once ``predicate`` holds (or after 20 s)."""
    import _thread
    import threading
    import time

    def go() -> None:
        deadline = time.monotonic() + 20.0
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        _thread.interrupt_main()

    threading.Thread(target=go, daemon=True).start()


def test_web_mode_parses():
    from backseat_harness.cli import _build_parser

    parser = _build_parser()
    assert parser.parse_args(["web"]).mode == "auto"
    assert parser.parse_args(["web", "--mode", "direct"]).mode == "direct"
    assert parser.parse_args(["web", "--mode=caddy"]).mode == "caddy"
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["web", "--mode", "bogus"])
    assert exc.value.code == 2


def test_web_auto_resolves_to_caddy_when_caddyfile_exists(fake_caddy, monkeypatch, tmp_path):
    """auto with a caddyfile runs caddy beside uvicorn and stops caddy before uvicorn."""
    from backseat_harness.cli import _build_parser
    from backseat_harness.web import caddy as caddy_mod

    events: list[str] = []
    real_start = caddy_mod.start

    def start(*args, **kwargs):
        events.append("caddy-start")
        handle = real_start(*args, **kwargs)
        real_stop = handle.stop

        def stop(timeout=caddy_mod._STOP_GRACE_S):
            events.append("caddy-stop")
            real_stop(timeout)

        handle.stop = stop
        return handle

    monkeypatch.setattr(caddy_mod, "start", start)
    _fake_uvicorn(monkeypatch, events)
    _write_web_caddyfile(_free_port(), _free_port())
    _interrupt_main_when(lambda: any(argv[0] == "run" for argv in fake_caddy.calls()))

    args = _build_parser().parse_args(["-C", str(tmp_path), "web", "--port", str(_free_port())])
    assert args.func(args) == 0

    assert [argv[0] for argv in fake_caddy.calls()] == ["validate", "run"]
    # uvicorn starts on its own thread, so only the stop order is fixed.
    assert "caddy-start" in events
    assert events[-2:] == ["caddy-stop", "uvicorn-stop"]


def test_web_direct_unchanged_without_caddyfile(fake_caddy, monkeypatch, tmp_path):
    """Direct mode serves uvicorn alone and never touches caddy."""
    from backseat_harness.cli import _build_parser

    events: list[str] = []
    started = _fake_uvicorn(monkeypatch, events)
    _interrupt_main_when(started.is_set)

    args = _build_parser().parse_args(["-C", str(tmp_path), "web", "--port", str(_free_port())])
    assert args.func(args) == 0

    assert fake_caddy.calls() == []
    assert events == ["uvicorn-start", "uvicorn-stop"]


def test_web_caddy_missing_is_exit_2_before_serving(fake_home, monkeypatch, tmp_path, capsys):
    """A caddyfile with no caddy on PATH is refused before uvicorn is created."""
    from backseat_harness.cli import _build_parser

    events: list[str] = []
    _fake_uvicorn(monkeypatch, events)
    _write_web_caddyfile(_free_port(), _free_port())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    args = _build_parser().parse_args(["-C", str(tmp_path), "web", "--port", str(_free_port())])
    rc = args.func(args)

    out, err = capsys.readouterr()
    assert rc == 2
    assert "not on PATH" in (out + err)
    assert events == []


def test_web_caddy_death_exits_nonzero(fake_caddy, monkeypatch, tmp_path, capsys):
    """Standalone web in caddy mode: caddy dies, the server stops, the exit is 1."""
    from backseat_harness.cli import _build_parser

    fake_caddy.mode("die_after")
    events: list[str] = []
    _fake_uvicorn(monkeypatch, events)
    _write_web_caddyfile(_free_port(), _free_port())

    port = _free_port()
    args = _build_parser().parse_args(
        ["-C", str(tmp_path), "web", "--mode", "caddy", "--port", str(port)]
    )
    rc = args.func(args)

    _out, err = capsys.readouterr()
    assert rc == 1
    assert "caddy exited rc=3" in err
    assert events == ["uvicorn-start", "uvicorn-stop"]


def test_takeover_clears_a_prior_run_journal(tmp_path, monkeypatch):
    from backseat_harness import storage
    from backseat_harness.config import TaskPaths

    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    paths = TaskPaths(root=tmp_path / LANE_A)
    storage.save_run_request(paths, "old", 1.0)
    _seat(monkeypatch, {os.getpid(): 5000.0}, [])
    monkeypatch.setattr(spawn, "run_interactive", lambda provider, native_id, cwd: 0)

    assert main(["-C", str(tmp_path), "takeover", "a", "--no-resume"]) == 0
    assert not paths.run_request.exists()


def test_takeover_writes_no_run_journal(tmp_path, monkeypatch):
    from backseat_harness.config import TaskPaths

    assert main(["-C", str(tmp_path), "init", "a", "Goal"]) == 0
    paths = TaskPaths(root=tmp_path / LANE_A)
    _seat(monkeypatch, {os.getpid(): 5000.0}, [])
    monkeypatch.setattr(spawn, "run_interactive", lambda provider, native_id, cwd: 0)

    assert main(["-C", str(tmp_path), "takeover", "a", "--no-resume"]) == 0
    assert not paths.run_request.exists()

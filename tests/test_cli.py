"""CLI lane creation: `run`'s second argument is the goal, or a brief."""

from backseat_harness import spawn
from backseat_harness.cli import _ensure_goal, main
from backseat_harness.config import load_config


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
        "t: continue after 47 iterations (max_iterations reached after 30 turns this run (47 total))"
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


def test_daemon_run_dispatches_to_foreground_handler(monkeypatch):
    import backseat_harness.cli as cli

    args = cli._build_parser().parse_args(["daemon", "run"])
    assert args.command == "daemon"
    assert args.daemon_command == "run"
    assert args.func == cli._cmd_daemon


def test_daemon_subcommands_parse_with_format_json():
    import backseat_harness.cli as cli

    cmds = [
        ["daemon", "status"],
        ["daemon", "stop"],
        ["daemon", "restart"],
        ["daemon", "install"],
    ]
    for cmd in cmds:
        args = cli._build_parser().parse_args(["--format", "json", *cmd])
        assert args.format == "json"
        assert args.command == "daemon"


def test_daemon_install_with_uninstall_and_path_override():
    import backseat_harness.cli as cli

    args = cli._build_parser().parse_args(
        ["daemon", "install", "--uninstall", "--path-override", "/tmp/custom"]
    )
    assert args.uninstall is True
    assert args.path_override == "/tmp/custom"
    assert args.func == cli._cmd_daemon_install


def test_daemon_status_running_registered(monkeypatch, capsys):
    import json

    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 1234, "start": 9999.0})
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: True})(),
    )
    rc = main(["--format", "json", "daemon", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["running"] is True
    assert payload["installed"] is True
    assert payload["pid"] == 1234
    assert payload["start"] == 9999.0
    assert payload["owns"] is True
    assert isinstance(payload.get("uptime"), (int, float))
    assert "owns" in payload


def test_daemon_status_not_installed(monkeypatch, capsys):
    import json

    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: False)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: False})(),
    )
    rc = main(["--format", "json", "daemon", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload == {"running": False, "installed": False}


def test_daemon_status_installed_not_running(monkeypatch, capsys):
    import json

    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: True})(),
    )
    rc = main(["--format", "json", "daemon", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload == {"running": False, "installed": True}
    assert "owns" not in payload


def test_daemon_status_unmanaged_live(monkeypatch, capsys):
    import json

    import backseat_harness.db as db_mod
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: {"pid": 777, "start": 3000.0})
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: True)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: False)
    monkeypatch.setattr(
        platform_mod,
        "artifact_path",
        lambda: type("P", (), {"exists": lambda s: False})(),
    )
    rc = main(["--format", "json", "daemon", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["running"] is True
    assert payload["installed"] is False
    assert payload["pid"] == 777
    assert payload["start"] == 3000.0
    assert payload["owns"] is False
    assert "owns" in payload
    assert isinstance(payload.get("uptime"), (int, float))


def test_daemon_stop_registered(monkeypatch, capsys):
    import backseat_harness.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    monkeypatch.setattr(platform_mod, "stop", lambda: None)
    rc = main(["daemon", "stop"])
    assert rc == 0


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

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    assert registered[0] is True
    assert started[0] is True
    payload = __import__("json").loads(capsys.readouterr().out)
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
    rc = main(["--format", "json", "daemon", "install"])
    assert rc == 0
    payload = __import__("json").loads(capsys.readouterr().out)
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

    monkeypatch.setattr(db_mod, "_daemon_record", lambda conn: None)
    monkeypatch.setattr(db_mod, "pid_live", lambda pid, start: False)
    monkeypatch.setattr(platform_mod, "is_registered", lambda: True)
    register_calls = [0]
    start_calls = [0]

    def spy_register():
        register_calls[0] += 1

    def spy_start():
        start_calls[0] += 1

    monkeypatch.setattr(platform_mod, "register", spy_register)
    monkeypatch.setattr(platform_mod, "start", spy_start)

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

"""CLI lane creation: a `run` meta-prompt argument bootstraps a task."""

from backseat_harness import spawn
from backseat_harness.cli import _ensure_meta, main
from backseat_harness.config import load_config


def test_run_requires_meta_for_new_task(tmp_path, capsys):
    rc = main(["-C", str(tmp_path), "run", "fresh"])
    assert rc == 2
    assert "second argument" in capsys.readouterr().out
    # Nothing is created on the error path.
    assert not (tmp_path / ".backseat").exists()


def test_run_accepts_positional_meta_for_new_task(tmp_path, monkeypatch):
    """`run slug "goal"` creates meta.md; `run slug` alone does not."""
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
    assert paths.meta.read_text(encoding="utf-8") == "Build the thing.\n"


def test_ensure_meta_creates_lane(tmp_path):
    paths = load_config(tmp_path).task_paths("fresh")
    assert _ensure_meta(paths, "  Add GET /hello.  ") is None
    assert paths.meta.read_text(encoding="utf-8") == "Add GET /hello.\n"


def test_ensure_meta_accepts_same_goal_and_existing_lane(tmp_path):
    paths = load_config(tmp_path).task_paths("t")
    _ensure_meta(paths, "Goal")
    assert _ensure_meta(paths, "Goal") is None
    assert _ensure_meta(paths, None) is None


def test_ensure_meta_rejects_different_goal(tmp_path):
    paths = load_config(tmp_path).task_paths("t")
    _ensure_meta(paths, "Goal")
    error = _ensure_meta(paths, "Other goal")
    assert error and "different meta-prompt" in error
    assert paths.meta.read_text(encoding="utf-8") == "Goal\n"


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
    paths.meta.write_text("Goal\n", encoding="utf-8")
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
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: (lambda p: ""))

    rc = main(["-C", str(ws), "takeover", "t"])
    assert rc == 0
    assert get_provider("opencode").interactive_command(seen["native_id"]) == [
        "opencode", "--session", "ses_live",
    ]
    assert seen["native_id"] == "ses_live"
    # Resume with no first_prompt: the operator re-plans from the advanced session.
    assert resumed["first_prompt"] is None


def test_takeover_no_resume_skips_loop(tmp_path, monkeypatch, capsys):
    import backseat_harness.cli as cli
    from backseat_harness.config import load_config

    paths = load_config(tmp_path).task_paths("t")
    paths.root.mkdir(parents=True)
    paths.meta.write_text("Goal\n", encoding="utf-8")

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
        slug="t", status=TaskStatus.BLOCKED, iterations=50, reason="max_iterations reached"
    )
    assert _result_line(out_of_budget).endswith("(max_iterations reached)")


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
    cli._make_operator_invoker(config, replace(config.operator, timeout_s=42))(
        "Do the review."
    )
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
            seen.update(argv=list(cmd))
            or spawn.CompletedCall(0, "ok", "", False)
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
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: (lambda p: ""))

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
    monkeypatch.setattr(cli, "_make_operator_invoker", lambda c, s: (lambda p: ""))
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
            exit_code=0, timed_out=False, duration_s=0.1,
            native_session_id="sess-1", stdout="", stderr="",
        )
        kwargs["operator_invoker"] = lambda p: '{"verdict":"done"}'
        return real_run_task(**kwargs)

    monkeypatch.setattr(cli, "run_task", cli_loop)
    monkeypatch.setattr(
        "backseat_harness.history.find_session", lambda search, provider: None
    )
    monkeypatch.setattr(
        "backseat_harness.history.fork_session", lambda search, provider: ""
    )
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
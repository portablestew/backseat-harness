"""CLI lane creation: a `run` meta-prompt argument bootstraps a task."""

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


def _capture_operator_spawn(monkeypatch, provider=None):
    """Stub the operator spawn end to end and capture the stdin prompt."""
    from conftest import FakeProvider

    import backseat_harness.cli as cli

    monkeypatch.setattr(cli, "get_provider", lambda name: provider or FakeProvider())
    monkeypatch.setattr(cli.spawn, "resolve_argv", lambda cmd: cmd)
    seen = {}

    class _Proc:
        stdout = "ok"

    def fake_run(cmd, **kwargs):
        seen["input"] = kwargs["input"]
        seen["argv"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
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

    class _Proc:
        stdout = "ok"

    def fake_run(cmd, **kwargs):
        seen["argv"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    try:
        op_config = replace(config.operator, agent="my-reviewer")
        cli._make_operator_invoker(config, op_config)("Do the review.")
    finally:
        monkeypatch.setattr(cli, "get_provider", real_get)
    assert seen["argv"][-2:] == ["--agent", "my-reviewer"]

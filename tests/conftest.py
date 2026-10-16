"""Shared fixtures. The loop is exercised with fakes so no live CLI is needed."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from backseat_harness.config import Config
from backseat_harness.providers.base import Provider, SpawnSpec


# --- slow gate -----------------------------------------------------------------
#
# Tests marked ``slow`` run a real daemon subprocess (~35 s) or a real caddy
# binary. They are skipped by default; run them with ``pytest --run-slow``
# (CI and pre-merge runs should pass the flag).


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="also run tests marked 'slow' (real daemon subprocess, real caddy)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: needs --run-slow (real subprocess or binary)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow test; pass --run-slow to run it")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


class FakeProvider(Provider):
    """A managed provider that touches nothing external and reports a fixed id."""

    name = "fake"
    history_provider = "fake_history"

    def __init__(self, native_id: str = "fake-session-1", transcript_size: int = 100):
        self._native_id = native_id
        self._size = transcript_size

    @property
    def transcript_size(self) -> int:
        """Size the stubbed history lookup should report for this provider."""
        return self._size

    def build_command(self, spec: SpawnSpec) -> list[str]:
        # Prompt goes on stdin, so it is absent from argv (see providers.base).
        return ["fake"]

    def parse_result(self, exit_code, stdout, stderr):
        return self._native_id

    def extract_text(self, stdout: str) -> str:
        return stdout

    def interactive_command(self, native_session_id):
        return ["fake"]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".backseat" / "tasks").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A throwaway home dir, distinct from the workspace, for the user layer."""
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def _hermetic_user_config(fake_home: Path, monkeypatch):
    """Keep the suite from ever reading a real ``~/.backseat/config.toml``.

    The loader resolves its default home through ``config._default_home``; pin
    that seam per test so direct ``load_config`` calls and ``main([...])`` CLI
    calls alike stay hermetic.
    """
    from backseat_harness import config as config_mod

    monkeypatch.setattr(config_mod, "_default_home", lambda: fake_home)


@pytest.fixture
def config(workspace: Path) -> Config:
    return Config(workspace_root=workspace, provider="fake")


@pytest.fixture
def stub_history(monkeypatch):
    """Stub the chat-mother-forker seam.

    Both the fork rendering and the session-size lookup read real chat history
    from real CLI sessions, which the loop tests must not depend on. Returning a
    size of 0 also keeps compaction switched off unless a test asks for it.
    """
    from backseat_harness import history

    state = {"fork": "", "session": None, "transcript": ""}

    monkeypatch.setattr(history, "fork_session", lambda search, provider: state["fork"])
    monkeypatch.setattr(history, "find_session", lambda search, provider: state["session"])
    monkeypatch.setattr(
        history, "load_full_transcript", lambda search, provider: state["transcript"]
    )
    return state


@pytest.fixture
def task(config: Config):
    """Create a task lane with a goal-prompt and empty checklist."""
    slug = "demo"
    paths = config.task_paths(slug)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.goal.write_text("Build a thing.\n", encoding="utf-8")
    paths.checklist.write_text(json.dumps({"steps": {}, "remarks": []}), encoding="utf-8")
    return slug, paths


# --- fake caddy ----------------------------------------------------------------

#: The stub caddy. It records every invocation (argv and pid) in
#: ``$CADDY_STUB_OUT/calls.jsonl`` and behaves by ``$CADDY_STUB_MODE``:
#: ``exit_on_stop`` exits on the graceful signal, ``hang`` ignores it,
#: ``spawn_child`` hangs and also starts a long-lived child (pid in child.pid),
#: ``die_after`` exits 3 straight away, ``validate_fail`` makes ``validate`` fail.
_CADDY_STUB = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

out = Path(os.environ["CADDY_STUB_OUT"])
mode = os.environ.get("CADDY_STUB_MODE", "exit_on_stop")
argv = sys.argv[1:]
with (out / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({"argv": argv, "pid": os.getpid()}) + "\n")

if argv[:1] == ["validate"]:
    if mode == "validate_fail":
        print("Error: unrecognized directive: bogus", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

graceful = signal.SIGBREAK if os.name == "nt" else signal.SIGTERM
hang = mode in ("hang", "spawn_child")


def on_graceful(signum, frame):
    if not hang:
        sys.exit(0)


signal.signal(graceful, on_graceful)
if mode == "die_after":
    sys.exit(3)
if mode == "spawn_child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    (out / "child.pid").write_text(str(child.pid))
(out / "run.pid").write_text(str(os.getpid()))
while True:
    time.sleep(0.2)
"""


class FakeCaddy:
    """Handle on the stub on PATH: switch its mode, read its recorded calls and pids."""

    def __init__(self, out: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.out = out
        self._monkeypatch = monkeypatch

    def mode(self, name: str) -> None:
        self._monkeypatch.setenv("CADDY_STUB_MODE", name)

    def calls(self) -> list[list[str]]:
        log = self.out / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]

    def pid(self, name: str) -> int:
        """Pid the stub wrote for ``run`` (``run.pid``) or its child (``child.pid``)."""
        return int((self.out / f"{name}.pid").read_text(encoding="utf-8"))


@pytest.fixture
def fake_caddy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCaddy:
    """A ``caddy`` executable on PATH that runs the stub under this interpreter."""
    bin_dir = tmp_path / "caddy_bin"
    bin_dir.mkdir()
    out = tmp_path / "caddy_out"
    out.mkdir()
    stub = bin_dir / "caddy_stub.py"
    stub.write_text(_CADDY_STUB, encoding="utf-8")
    python = sys.executable
    if os.name == "nt":
        (bin_dir / "caddy.bat").write_text(
            f'@echo off\r\n"{python}" "{stub}" %*\r\n', encoding="utf-8"
        )
    else:
        exe = bin_dir / "caddy"
        exe.write_text(f'#!/bin/sh\nexec "{python}" "{stub}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CADDY_STUB_OUT", str(out))
    monkeypatch.setenv("CADDY_STUB_MODE", "exit_on_stop")
    return FakeCaddy(out, monkeypatch)

"""Shared fixtures. The loop is exercised with fakes so no live CLI is needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backseat_harness.config import Config
from backseat_harness.providers.base import Provider, SpawnSpec


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


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".backseat" / "tasks").mkdir(parents=True)
    return tmp_path


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

    state = {"fork": "", "session": None}

    monkeypatch.setattr(history, "fork_session", lambda search, provider: state["fork"])
    monkeypatch.setattr(history, "find_session", lambda search, provider: state["session"])
    return state


@pytest.fixture
def task(config: Config):
    """Create a task lane with a meta-prompt and empty checklist."""
    slug = "demo"
    paths = config.task_paths(slug)
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text("Build a thing.\n", encoding="utf-8")
    paths.checklist.write_text(json.dumps({"steps": {}, "remarks": []}), encoding="utf-8")
    return slug, paths

"""The Generate Task button and POST on the chat transcript page.

Hermetic like test_web.py: the db is pinned to a throwaway home by conftest, the
chat store is stubbed through history's seams, and the spawn is a fake Popen
that records its arguments. Nothing runs the generate-task CLI, and nothing
waits on the child.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backseat_harness import db, history, manager
from backseat_harness.web.main import create_app

PORT = 8080
KEY = "opencode:ses_gen"
TRANSCRIPT = "User: keep it to the token budget guard\nAssistant: noted"


class FakeChild:
    """Stands in for the Popen handle. Any wait would mean the request blocked on it."""

    def __init__(self) -> None:
        self.waited = False

    def wait(self, *args, **kwargs):
        self.waited = True
        raise AssertionError("the generate-task child must not be awaited")

    def communicate(self, *args, **kwargs):
        self.waited = True
        raise AssertionError("the generate-task child must not be awaited")

    def poll(self):
        return None


@pytest.fixture
def spawned(monkeypatch):
    """Record every Popen call. Each returns a FakeChild and runs nothing."""
    calls: list[tuple[list[str], dict, FakeChild]] = []

    def record(cmd, **kwargs):
        child = FakeChild()
        calls.append((cmd, kwargs, child))
        return child

    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


@pytest.fixture
def chat(monkeypatch):
    """A readable chat."""
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: TRANSCRIPT for k in keys}
    )


def _client() -> TestClient:
    return TestClient(create_app(port=PORT), base_url=f"http://localhost:{PORT}")


def _register(workspace: Path) -> int:
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True
        ws_id = db._workspace_id(conn, workspace)
    assert ws_id is not None
    return ws_id


def _post(client: TestClient, provider: str, conversation_id: str, **data):
    return client.post(
        f"/chat/{provider}/{conversation_id}/generate-task",
        data=data,
        follow_redirects=False,
    )


def _gen_dirs(workspace: Path) -> list[Path]:
    return sorted((workspace / ".backseat" / "tasks").glob(f"{manager.STAGING_PREFIX}*"))


def _confirm_message(html: str) -> str:
    """The dialog text: the JSON literal the template passes to ``window.confirm``."""
    match = re.search(r"const message = (.+);", html)
    assert match, "no confirm message in the page"
    return json.loads(match.group(1))


# --- button presence and absence -----------------------------------------------


@pytest.mark.parametrize(
    "path, next_button",
    [
        ("/chat/opencode/ses_gen", "Resume"),  # provider with resume
        ("/chat/cline/sess_fork", "Fork"),  # provider without resume
    ],
    ids=["with-resume", "without-resume"],
)
def test_button_shows_left_of_the_next_header_button(workspace, chat, monkeypatch, path, next_button):
    if next_button == "Fork":
        monkeypatch.setattr(
            history, "load_sessions", lambda keys, providers=None: {k: "ide body" for k in keys}
        )
    ws_id = _register(workspace)

    with _client() as client:
        html = client.get(f"{path}?ws={ws_id}").text

    assert 'id="generate-task-form"' in html
    assert f'action="{path}/generate-task"' in html
    assert re.search(rf'name="ws" value="{ws_id}"', html)
    assert html.index(">✨ Task<") < html.index(f">{next_button}<")


def test_action_buttons_share_a_right_aligned_group(workspace, chat):
    """The header actions sit in one group, so they stay at the right edge whatever
    the prompt's length. The Generate button carries its own amber class."""
    ws_id = _register(workspace)

    with _client() as client:
        html = client.get(f"/chat/opencode/ses_gen?ws={ws_id}").text
        css = client.get("/").text

    group = html[html.index('class="chat-header-actions"') : html.index("</header>")]
    assert 'id="generate-task-form"' in group and 'id="resume-form"' in group
    assert 'class="btn generate-task-btn"' in group
    rule = css[css.index(".chat-header-actions {") :]
    assert "margin-left: auto" in rule[: rule.index("}")]


def test_button_is_hidden_without_a_workspace(chat):
    with _client() as client:
        html = client.get("/chat/opencode/ses_gen").text

    assert 'id="generate-task-form"' not in html
    assert "✨ Task" not in html


def test_button_shows_for_a_chat_written_just_now(workspace, monkeypatch):
    """No liveness rule: a chat written a moment ago still offers Generate Task."""
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: TRANSCRIPT for k in keys}
    )
    ws_id = _register(workspace)

    with _client() as client:
        html = client.get(f"/chat/opencode/ses_gen?ws={ws_id}").text

    assert 'id="generate-task-form"' in html


def test_dialog_states_the_rules_and_names_the_workspace_path(workspace, chat):
    ws_id = _register(workspace)

    with _client() as client:
        html = client.get(f"/chat/opencode/ses_gen?ws={ws_id}").text

    message = _confirm_message(html)
    assert "Task requirements will be automatically generated from this conversation." in message
    assert (
        "Ensure the chat is finished and the ideas are thoroughly fleshed out before you proceed."
        in message
    )
    assert "a few minutes" not in message
    assert f"Target workspace: {workspace.name}\nPath: {workspace.resolve()}" in message


# --- the endpoint ---------------------------------------------------------------


def test_valid_post_spawns_a_detached_generate_task_and_redirects(workspace, chat, spawned):
    ws_id = _register(workspace)
    ws_arg = str(workspace.resolve())

    with _client() as client:
        resp = _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="yes")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ws/{ws_id}"
    assert len(spawned) == 1
    cmd, kwargs, child = spawned[0]
    dirs = _gen_dirs(workspace)
    assert len(dirs) == 1
    assert cmd == [
        sys.executable,
        "-m",
        "backseat_harness.cli",
        "-C",
        ws_arg,
        "generate-task",
        KEY,
        "--staging",
        dirs[0].name,
    ]
    assert kwargs["cwd"] == ws_arg
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    if sys.platform == "win32":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True
    assert child.waited is False


def test_the_staging_dir_is_queued_before_the_child_starts(workspace, chat, spawned):
    ws_id = _register(workspace)

    with _client() as client:
        _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="yes")

    (staging,) = _gen_dirs(workspace)
    manifest = manager.read_manifest(staging)
    assert manifest["status"] == "queued"
    assert manifest["source_key"] == KEY
    assert manifest["workspace"] == str(workspace.resolve())


def test_post_without_confirmation_is_400_and_spawns_nothing(workspace, chat, spawned):
    ws_id = _register(workspace)

    with _client() as client:
        resp = _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="")

    assert resp.status_code == 400
    assert spawned == []
    assert _gen_dirs(workspace) == []


def test_unregistered_workspace_is_404_and_spawns_nothing(workspace, chat, spawned):
    with _client() as client:
        resp = _post(client, "opencode", "ses_gen", ws=999, confirmed="yes")

    assert resp.status_code == 404
    assert spawned == []
    assert _gen_dirs(workspace) == []


def test_unknown_provider_is_404_and_spawns_nothing(workspace, chat, spawned):
    ws_id = _register(workspace)

    with _client() as client:
        resp = _post(client, "not_a_provider", "ses_gen", ws=ws_id, confirmed="yes")

    assert resp.status_code == 404
    assert spawned == []


def test_bad_session_id_is_404_and_spawns_nothing(workspace, chat, spawned, monkeypatch):
    monkeypatch.setattr(history, "load_sessions", lambda keys, providers=None: {})
    ws_id = _register(workspace)

    with _client() as client:
        resp = _post(client, "opencode", "ses_missing", ws=ws_id, confirmed="yes")

    assert resp.status_code == 404
    assert spawned == []
    assert _gen_dirs(workspace) == []


def test_concurrent_submits_for_one_chat_spawn_exactly_once(workspace, chat, spawned, monkeypatch):
    """Double-submit race (4c note): six POSTs at once must spawn one generation.

    The window is between the active check and the staging create. The create is
    slowed so every submit passes the check before any staging dir exists. Without
    a lock around check-and-create, all six pass and spawn.
    """
    ws_id = _register(workspace)
    real_new_staging = manager.new_staging

    def slow_new_staging(*args, **kwargs):
        time.sleep(0.05)
        return real_new_staging(*args, **kwargs)

    monkeypatch.setattr(manager, "new_staging", slow_new_staging)
    barrier = threading.Barrier(6)
    statuses: list[int] = []

    def submit():
        barrier.wait()
        with _client() as client:
            resp = _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="yes")
        statuses.append(resp.status_code)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(statuses) == [303, 409, 409, 409, 409, 409]
    assert len(spawned) == 1
    assert len(_gen_dirs(workspace)) == 1


def test_a_running_generation_for_the_same_chat_is_409(workspace, chat, spawned):
    ws_id = _register(workspace)
    staging = manager.new_staging(workspace / ".backseat" / "tasks", KEY, str(workspace))
    data = manager.read_manifest(staging)
    data.update(status="running", pid=os.getpid(), pid_start=db.process_start(os.getpid()))
    manager.write_manifest(staging, data)

    with _client() as client:
        resp = _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="yes")

    assert resp.status_code == 409
    assert spawned == []


def test_an_interrupted_generation_does_not_block_a_new_one(workspace, chat, spawned):
    """A dead pid reads as interrupted, so it must not block a fresh submit."""
    ws_id = _register(workspace)
    staging = manager.new_staging(workspace / ".backseat" / "tasks", KEY, str(workspace))
    data = manager.read_manifest(staging)
    data.update(status="running", pid=999999, pid_start=1.0)
    manager.write_manifest(staging, data)

    with _client() as client:
        resp = _post(client, "opencode", "ses_gen", ws=ws_id, confirmed="yes")

    assert resp.status_code == 303
    assert len(spawned) == 1



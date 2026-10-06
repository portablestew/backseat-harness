"""Web app tests (step 9.1-9.4): allowlist, homepage, add-task, search, shell.

Hermetic: tests/conftest.py's autouse home pin routes ``config._default_home``
-- through which ``db.db_path`` resolves on every call -- at a throwaway dir,
so the sqlite registry these tests write is never the real
``~/.backseat/db.sqlite``. No server is ever started; everything runs through
FastAPI's TestClient against real response statuses and bodies. The only
stubbed seam for /search is chat-mother-forker's history loader (an external
provider layer); classification, fork-provider choice, and rendering are real.
Shell tests drive the real PTYSessionManager and a real child process.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backseat_harness import db
from backseat_harness.web.main import create_app
from backseat_harness.web.services import chat_search as chat_search_service

_PORT = 8080


def _client(host: str = "localhost") -> TestClient:
    """TestClient whose base_url makes the sent Host header allowlistable."""
    return TestClient(create_app(port=_PORT), base_url=f"http://{host}:{_PORT}")


# --- Host/Origin allowlist (step 3.2) ------------------------------------------


def test_localhost_host_header_is_accepted():
    with _client("localhost") as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Workspaces" in resp.text


def test_loopback_ip_host_header_is_accepted():
    with _client("127.0.0.1") as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Workspaces" in resp.text


def test_foreign_host_header_is_rejected():
    with _client("evil.com") as client:
        resp = client.get("/")
    assert resp.status_code == 403
    assert "forbidden host" in resp.text


def test_foreign_origin_on_post_is_rejected():
    with _client("localhost") as client:
        resp = client.post(
            "/workspaces",
            data={"path": "/nowhere"},
            headers={"Origin": "http://evil.com"},
        )
    assert resp.status_code == 403
    assert "forbidden origin" in resp.text


# --- homepage workspace list (step 4.3, V4) ------------------------------------


def test_homepage_lists_registered_workspace_name_and_full_path(workspace: Path):
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True

    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    resolved = str(workspace.resolve())
    name = Path(resolved).name
    # V4 card shape: bold trailing dir name over the full path.
    assert f"<strong>{name}</strong>" in resp.text
    assert f"<small>{resolved}</small>" in resp.text


def test_homepage_without_workspaces_says_so():
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "No workspaces registered yet." in resp.text


# --- add-task form (step 5.3, 9.2) ---------------------------------------------


def _register_workspace(workspace: Path) -> int:
    """Insert the workspace in the temp registry; return its url id."""
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True
        ws_id = db._workspace_id(conn, workspace)
    assert ws_id is not None
    return ws_id


def _post_task(ws_id: int, name: str, goal: str):
    """POST the add-task form without following the redirect."""
    with _client() as client:
        return client.post(
            f"/ws/{ws_id}/tasks",
            data={"name": name, "goal": goal},
            follow_redirects=False,
        )


def _tree(root: Path) -> set[Path]:
    """Every path under ``root`` (files and dirs), relative to it."""
    return {p.relative_to(root) for p in root.rglob("*")}


def test_add_task_sanitizes_name_and_writes_goal(workspace: Path):
    ws_id = _register_workspace(workspace)
    name = "My Task! Ünïcodé / part"
    goal = "Ship the feature\nwith a second line."

    resp = _post_task(ws_id, name, goal)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ws/{ws_id}"

    tasks_dir = workspace / ".backseat" / "tasks"
    lanes = [p for p in tasks_dir.iterdir() if p.is_dir()]
    assert len(lanes) == 1

    slug = lanes[0].name
    assert slug == "my-task-unicode-part"
    # A valid dirname: no path separators, no "..", one portable charset.
    assert re.fullmatch(r"[a-z0-9_-]+", slug)
    assert "/" not in slug and "\\" not in slug
    assert slug not in (".", "..")

    goal_path = tasks_dir / slug / "goal.md"
    assert goal_path.is_file()
    assert goal_path.read_text(encoding="utf-8") == goal + "\n"
    # The lane contains the goal file only — nothing else was invented.
    assert {p.name for p in (tasks_dir / slug).iterdir()} == {"goal.md"}


def test_add_task_path_traversal_name_stays_inside_tasks(workspace: Path):
    ws_id = _register_workspace(workspace)
    before = _tree(workspace)

    resp = _post_task(ws_id, "../evil", "escape attempt")

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/ws/{ws_id}"

    # Nothing appeared outside .backseat/tasks/.
    created = _tree(workspace) - before
    assert created == {
        Path(".backseat/tasks/evil"),
        Path(".backseat/tasks/evil/goal.md"),
    }
    assert not (workspace.parent / "evil").exists()

    tasks_dir = workspace / ".backseat" / "tasks"
    lanes = [p for p in tasks_dir.iterdir() if p.is_dir()]
    assert len(lanes) == 1
    slug = lanes[0].name
    assert slug == "evil"
    assert re.fullmatch(r"[a-z0-9_-]+", slug)
    assert slug not in (".", "..")

    goal_path = tasks_dir / slug / "goal.md"
    assert goal_path.is_file()
    assert goal_path.read_text(encoding="utf-8") == "escape attempt\n"


# --- /search page (step 6.1-6.4, 9.3) ------------------------------------------

_MARKER = "<meta>START SESSION 5bee533f-ab27-4fe2-a548-a675f597479c #1</meta>"


class _Ref:
    """Stand-in for a chat-mother-forker ChatRef (only the attrs the seam reads)."""

    def __init__(self, key: str, mtime: float, project: str | None = None):
        self.key = key
        self.mtime = mtime
        self.project = project


def _stub_chat_history(monkeypatch, refs, transcripts) -> None:
    """Replace chat-mother-forker's loader with a fixed ref/transcript set.

    Everything downstream -- search_all_sessions' sort, classify_session, the
    route's resume/fork enrichment, and the template -- stays real.
    """
    from backseat_harness import history

    def _fake_load(attr: str):
        if attr == "find_chats":
            return lambda query="", providers=None, limit=None: list(refs)
        if attr == "default_providers":
            return lambda: []
        if attr == "load_chat":
            return lambda key, providers=None: transcripts[key]
        raise AssertionError(f"unexpected history seam requested: {attr}")

    monkeypatch.setattr(history, "_load", _fake_load)


def _data_rows(html: str) -> list[str]:
    """Body rows of the results table (header row uses <th>, not <td>)."""
    return [row for row in re.findall(r"<tr>.*?</tr>", html, re.S) if "<td>" in row]


def test_search_page_classifies_sessions_and_renders_one_action_per_row(monkeypatch):
    refs = [
        _Ref("opencode:ses_backseat", mtime=300.0),
        _Ref("opencode:ses_plain", mtime=200.0),
        _Ref("kiro_ide:sess_k1", mtime=100.0),
    ]
    transcripts = {
        "opencode:ses_backseat": f"working on the harness\n{_MARKER}\nmore text",
        "opencode:ses_plain": "a plain chat about refactors",
        "kiro_ide:sess_k1": "an IDE chat with no markers",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search")

    assert resp.status_code == 200
    rows = _data_rows(resp.text)
    assert len(rows) == 3

    # (a1) marker-bearing session is classified as a backseat task.
    backseat_row = next(r for r in rows if "ses_backseat" in r)
    assert '<span class="badge">backseat task</span>' in backseat_row

    # (a2) supported provider renders Resume with the 7.5 interactive argv;
    # classification stays "other" (no marker), i.e. nothing is blanket-labeled.
    resume_row = next(r for r in rows if "ses_plain" in r)
    assert '<span class="badge">other</span>' in resume_row
    assert '["opencode", "--session", "ses_plain"]' in resume_row
    assert ">Resume<" in resume_row
    assert "Fork" not in resume_row  # exactly one action per row

    # (a3) non-supported provider renders Fork instead, carrying the row key.
    fork_row = next(r for r in rows if "sess_k1" in r)
    assert 'action="/search/fork"' in fork_row
    assert 'name="current_session_key" value="kiro_ide:sess_k1"' in fork_row
    assert ">Fork<" in fork_row
    assert "Resume" not in fork_row


def _fork_result(key: str, mtime: float) -> chat_search_service.SessionSearchResult:
    return chat_search_service.SessionSearchResult(
        key=key,
        native_id=key.partition(":")[2],
        project=None,
        mtime=mtime,
        preview="",
        classification="other",
    )


def test_pick_fork_provider_maps_kiro_ide_to_kiro(monkeypatch):
    # Rule 1 fires before the PATH scan, so nothing needs to be installed.
    monkeypatch.setattr("shutil.which", lambda exe: None)

    assert chat_search_service.pick_fork_provider("kiro_ide:sess_1", []) == "kiro"
    assert chat_search_service.pick_fork_provider("kiro_ide_v2:sess_1", []) == "kiro"


def test_pick_fork_provider_most_recent_supported_chat_wins(monkeypatch):
    # Input order = newest first, the order search_all_sessions guarantees.
    results = [
        _fork_result("cursor:sess_newest_unsupported", 400.0),
        _fork_result("claude_code:sess_mid", 300.0),
        _fork_result("opencode:sess_old", 100.0),
    ]
    monkeypatch.setattr("shutil.which", lambda exe: None)  # PATH must not decide

    assert (
        chat_search_service.pick_fork_provider("unknown:sess_cur", results) == "claude"
    )

    # kiro_ide maps to the harness 'kiro' (accepted 6.3 mapping), so a newest
    # kiro_ide chat is itself the most recent supported chat.
    kiro_newest = [
        _fork_result("kiro_ide:sess_k", 500.0),
        _fork_result("claude_code:sess_c", 300.0),
    ]
    assert (
        chat_search_service.pick_fork_provider("unknown:sess_cur", kiro_newest)
        == "kiro"
    )


def test_pick_fork_provider_falls_back_to_first_path_match(monkeypatch):
    results = [_fork_result("unknown:sess_cur", 1.0)]

    # Only kiro-cli and claude are installed -> kiro wins the scan order.
    monkeypatch.setattr(
        "shutil.which", lambda exe: "/usr/bin/" + exe if exe != "opencode" else None
    )
    assert chat_search_service.pick_fork_provider("unknown:sess_cur", results) == "kiro"

    # All installed -> opencode is first in scan order.
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)
    assert chat_search_service.pick_fork_provider("unknown:sess_cur", results) == "opencode"


def test_pick_fork_provider_raises_when_nothing_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda exe: None)

    with pytest.raises(RuntimeError, match="no supported CLI"):
        chat_search_service.pick_fork_provider("unknown:sess_cur", [])


# --- shell routes (step 8.3-8.4, 9.4) ------------------------------------------

_SHELL_MARKER = "SHELL94_MARKER_OK"


def _shell_manager():
    from backseat_harness.web.routes import shell as shell_routes

    return shell_routes.get_manager()


def _start_session(client: TestClient, cmd: list[str], cwd: Path) -> str:
    """POST /shell/start for a real child; return its session id."""
    resp = client.post("/shell/start", json={"cmd": cmd, "cwd": str(cwd)})
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    assert re.fullmatch(r"[0-9a-f]{12}", session_id)
    return session_id


def _kill_session(session_id: str) -> None:
    """Terminate this session's child and drop it — no process-wide kills."""
    manager = _shell_manager()
    session = manager.get_session(session_id)
    if session is not None:
        session.backend.kill()
    manager.cleanup_stale()


def _wait_ring_buffer(session_id: str, marker: bytes, timeout: float = 20.0) -> bytes:
    """Poll the real PTY reader's ring buffer until the marker lands in it."""
    manager = _shell_manager()
    deadline = time.monotonic() + timeout
    snapshot = b""
    while time.monotonic() < deadline:
        session = manager.get_session(session_id)
        if session is not None:
            with session.lock:
                snapshot = b"".join(session.buffer)
            if marker in snapshot:
                return snapshot
        time.sleep(0.05)
    raise AssertionError(f"marker never reached the ring buffer; buffer={snapshot!r}")


def _receive_until(ws, marker: bytes, limit: int = 20) -> bytes:
    """Collect binary frames until the marker appears (bounded, no hang)."""
    got = b""
    for _ in range(limit):
        got += ws.receive_bytes()
        if marker in got:
            return got
    return got


def test_shell_start_and_page_render_real_html(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(f"/shell/{session_id}")
            assert page.status_code == 200
            assert "text/html" in page.headers["content-type"]
            assert f'const wsPath = "/ws/shell/{session_id}";' in page.text
            assert "xterm" in page.text

            assert client.get("/shell/000000000000").status_code == 404
        finally:
            _kill_session(session_id)


def test_shell_websocket_streams_marker_and_second_attach_replays(tmp_path: Path):
    marker = _SHELL_MARKER.encode()
    cmd = [
        sys.executable,
        "-c",
        f"import time; print('{_SHELL_MARKER}'); time.sleep(120)",
    ]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            # Real child output reached the real reader thread's ring buffer.
            _wait_ring_buffer(session_id, marker)

            with client.websocket_connect(
                f"ws://localhost:{_PORT}/ws/shell/{session_id}"
            ) as ws:
                assert marker in _receive_until(ws, marker)

            session = _shell_manager().get_session(session_id)
            assert session is not None
            with session.lock:
                before = b"".join(session.buffer)
            assert marker in before  # the marker is banked in the ring buffer

            # The child printed once and has been sleeping since: if no new
            # marker output appears, a second attach can only be replay.
            with client.websocket_connect(
                f"ws://localhost:{_PORT}/ws/shell/{session_id}"
            ) as ws:
                assert marker in _receive_until(ws, marker)

            with session.lock:
                after = b"".join(session.buffer)
            assert marker not in after[len(before):]
        finally:
            _kill_session(session_id)


def test_shell_websocket_rejects_foreign_origin_and_host(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        url = f"ws://localhost:{_PORT}/ws/shell/{session_id}"
        try:
            # Positive control: an allowed Origin still connects.
            with client.websocket_connect(
                url, headers={"origin": f"http://localhost:{_PORT}"}
            ):
                pass

            # A foreign Origin must not be accepted (WebSocketDisconnect is
            # raised on close-before-accept, denial responses subclass it).
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    url, headers={"origin": "http://evil.com"}
                ):
                    pass

            # Same for a foreign Host header.
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(url, headers={"host": "evil.com"}):
                    pass
        finally:
            _kill_session(session_id)

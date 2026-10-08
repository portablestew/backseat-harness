"""Web app tests (steps 9.1-9.4, 11.1-11.4): allowlist, homepage, add-task,
search, shell, plus the step-11 gap closures (auto-suggest, workspace page
tables, task viewer controls, chat viewer resume + fork prompt).

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

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backseat_harness import db
from backseat_harness.web.main import create_app
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import workspace as workspace_service

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


def test_homepage_and_fragment_link_workspace_cards(workspace: Path):
    """Each workspace card links to ``/ws/<id>`` in both the full page and the
    60s HTMX fragment (docs/2-plan.md Step A).

    Regression: cards were bare ``<article>`` elements with no ``href`` and
    ``_entries()`` carried no id, so workspaces could not be clicked.
    """
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True
        ws_id = db._workspace_id(conn, workspace)
    assert ws_id is not None

    with _client() as client:
        page = client.get("/")
        fragment = client.get("/workspaces/fragment")

    assert page.status_code == 200
    assert fragment.status_code == 200
    link = f'href="/ws/{ws_id}"'
    assert link in page.text
    assert link in fragment.text
    # The old non-clickable markup is gone from both renders.
    assert '<article class="workspace-card">' not in page.text
    assert '<article class="workspace-card">' not in fragment.text


def test_homepage_without_workspaces_says_so():
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "No workspaces registered yet." in resp.text


def test_base_uses_dark_theme():
    """The shared layout is unconditionally dark: Pico's ``data-theme="dark"``
    pins the palette regardless of the OS and ``color-scheme: dark`` locks
    native widgets dark (plan Step D, Q8/F7)."""
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert 'data-theme="dark"' in resp.text
    assert "color-scheme: dark" in resp.text
    # No light variant and no OS-driven theme switching.
    assert "prefers-color-scheme" not in resp.text


def test_base_swaps_400_error_toast():
    """A 400 from add-workspace must reach ``#toast``: the base layout opts 400
    responses into htmx's swap (htmx does not swap 4xx by default)."""
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "htmx:beforeSwap" in resp.text
    assert "=== 400" in resp.text


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
    """Stand-in for a chat-mother-forker ChatRef (attrs the seams read)."""

    def __init__(
        self,
        key: str,
        mtime: float,
        project: str | None = None,
        *,
        locator: str | None = None,
        database: str | None = None,
        session: str | None = None,
    ):
        self.key = key
        self.mtime = mtime
        self.project = project
        self.locator = locator
        self.database = database
        self.session = session


def _stub_chat_history(monkeypatch, refs, transcripts) -> None:
    """Replace chat-mother-forker's loader with a fixed ref/transcript set.

    Everything downstream -- search_all_sessions' sort, classify_session, the
    route's resume/fork enrichment, and the template -- stays real.

    Three seams are stubbed, one per shape of read:
    - ``find_chats`` (the parse-per-row search) for the two listing pages,
    - ``list_session_refs`` (the cheap, unparsed listing) for the suggestion
      list and the fork route,
    - ``load_sessions`` (batched transcripts) for the rows' text.

    ``load_chat`` is deliberately *not* stubbed: a request for it means some
    path went back to the per-row tiered search that re-scans every stored
    conversation for each row, which is what made ``/ws/<id>`` take minutes
    (history.load_sessions).
    """
    from backseat_harness import history

    def _fake_load(attr: str):
        if attr == "find_chats":
            return lambda query="", providers=None, limit=None: list(refs)
        if attr == "default_providers":
            return lambda: []
        raise AssertionError(f"unexpected history seam requested: {attr}")

    monkeypatch.setattr(history, "_load", _fake_load)
    monkeypatch.setattr(history, "list_session_refs", lambda limit=None: list(refs))
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: transcripts[k] for k in keys if k in transcripts},
    )


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
    # Strict browser-style parse: the cmd attribute must survive attribute
    # extraction as one JSON value (the double-quoted tojson bug).
    assert json.loads(_hidden_input(resume_row, "cmd")) == [
        "opencode",
        "--session",
        "ses_plain",
    ]

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


def test_shell_start_accepts_initial_pty_size(tmp_path: Path):
    """POST /shell/start with optional cols/rows sizes the PTY at spawn
    (plan 3g): a real child reports the OS-view of its own terminal."""
    probe = "COLSROWS_OK"
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys, time; time.sleep(1); "
            "s = os.get_terminal_size(sys.stdout.fileno()); "
            f"print('{probe}' + ':' + str(s.columns) + 'x' + str(s.lines), flush=True); "
            "time.sleep(60)"
        ),
    ]
    with _client() as client:
        resp = client.post(
            "/shell/start",
            json={"cmd": cmd, "cwd": str(tmp_path), "cols": 123, "rows": 41},
        )
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]
        try:
            snapshot = _wait_ring_buffer(
                session_id, f"{probe}:123x41".encode(), timeout=10.0
            )
            assert f"{probe}:123x41".encode() in snapshot
        finally:
            _kill_session(session_id)


def test_shell_page_sends_resize_on_open(tmp_path: Path):
    """The served shell page (plan 3g): the WS onopen handler emits a resize
    frame with the live term sizes, and onResize is registered before the
    first fit() so fit's own frame is not dropped."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(f"/shell/{session_id}")
            assert page.status_code == 200
            js = page.text

            # onResize registered BEFORE the first fit() call.
            assert js.index("term.onResize") < js.index("fitAddon.fit()")

            # onopen announces the real terminal size to the PTY.
            open_idx = js.index("ws.onopen")
            open_resize_idx = js.index("cols: term.cols, rows: term.rows")
            assert 0 < open_idx < open_resize_idx
        finally:
            _kill_session(session_id)


def test_chat_continue_cwd_is_the_chat_workspace_when_known(
    monkeypatch, workspace: Path
):
    """Goal G7: the continue button launches the web shell in the chat's own
    workspace (the catches cwd='.', the server's cwd, not the chat's). Falls
    back to '.' when the referral carries no ?ws=<id>."""
    from backseat_harness import history

    with db.open_db() as conn:
        db.add_workspace(conn, workspace)
        ws_id = db._workspace_id(conn, workspace)

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "chat body" for k in keys},
    )

    with _client() as client:
        resp = client.get(f"/chat/opencode/ses_cwd?ws={ws_id}")
        assert _hidden_input(resp.text, "cwd") == str(workspace.resolve())

        resp2 = client.get("/chat/opencode/ses_cwd")
        assert _hidden_input(resp2.text, "cwd") == "."


# --- step 11a: homepage auto-suggest -------------------------------------------


def test_homepage_suggest_offers_only_unregistered_chat_dirs(
    monkeypatch, tmp_path: Path
):
    """GET /workspaces/suggest -- the homepage's Suggestions button -- lists
    chat-bearing dirs that are not registered, and nothing else."""
    candidate = tmp_path / "candidate-ws"
    registered = tmp_path / "already-registered"
    vanished = tmp_path / "never-created"
    candidate.mkdir()
    registered.mkdir()
    with db.open_db() as conn:
        assert db.add_workspace(conn, registered) is True

    refs = [
        _Ref("opencode:ses_in_candidate", mtime=300.0, project=str(candidate)),
        _Ref("claude_code:ses_in_registered", mtime=200.0, project=str(registered)),
        _Ref("opencode:ses_vanished", mtime=100.0, project=str(vanished)),
    ]
    _stub_chat_history(monkeypatch, refs, {})

    with _client() as client:
        page = client.get("/")
        resp = client.get("/workspaces/suggest")

    # The homepage add control is wired to this endpoint.
    assert 'hx-get="/workspaces/suggest"' in page.text

    assert resp.status_code == 200
    body = resp.text
    # Offered: the unregistered dir with chats, as a datalist option.
    candidate_s = workspace_service._normalize(candidate)
    assert f'<option value="{candidate_s}">' in body
    # The old POST-button payload is gone (Step C changed the shape, F2).
    assert 'hx-post="/workspaces"' not in body
    # Filtered out: already-registered workspaces and dirs that don't exist.
    assert str(registered) not in body
    assert str(vanished) not in body


def test_workspace_dir_for_dispatches_kiro_ide_and_v2(tmp_path: Path):
    """Exact provider dispatch: ``kiro_ide`` and ``kiro_ide_v2`` recover from
    their own (different) store shapes (plan D2, F3)."""
    v2_ws = tmp_path / "v2-workspace"
    v2_ws.mkdir()
    v2_session = tmp_path / "v2-session"
    v2_session.mkdir()
    (v2_session / "session.json").write_text(
        json.dumps({"workspacePaths": [str(v2_ws)]}), encoding="utf-8"
    )

    legacy_ws = tmp_path / "legacy-workspace"
    legacy_ws.mkdir()
    legacy_log = tmp_path / "exec.json"
    legacy_log.write_text(
        json.dumps(
            {
                "context": {
                    "messages": [
                        {
                            "entries": [
                                {
                                    "type": "document",
                                    "document": {
                                        "staticDirectoryView": (
                                            f"<folder name='{legacy_ws}\\.git' closed />"
                                        )
                                    },
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    v2_ref = _Ref(
        "kiro_ide_v2:sess_1", 2.0, v2_ws.name, locator=str(v2_session)
    )
    legacy_ref = _Ref(
        "kiro_ide:sess_2", 1.0, legacy_ws.name, locator=str(legacy_log)
    )

    assert workspace_service._workspace_dir_for(v2_ref) == str(v2_ws)
    assert workspace_service._workspace_dir_for(legacy_ref) == str(legacy_ws)


def test_workspace_dir_for_reads_stores_and_skips_unrecoverable(tmp_path: Path):
    """DB/jsonl recovery succeeds; unknown/missing store data returns None
    (never a guessed path) (plan Q3/Q4, F4)."""
    sqlite_ws = tmp_path / "sqlite-workspace"
    sqlite_ws.mkdir()
    db_file = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
    conn.execute("INSERT INTO session VALUES (?, ?)", ("ses_1", str(sqlite_ws)))
    conn.commit()
    conn.close()
    db_ref = _Ref(
        "opencode:ses_1", 1.0, sqlite_ws.name, database=str(db_file), session="ses_1"
    )
    assert workspace_service._workspace_dir_for(db_ref) == str(sqlite_ws)

    # claude_code: the first line has no cwd; a later line does (F4).
    claude_ws = tmp_path / "claude-workspace"
    claude_ws.mkdir()
    jsonl = tmp_path / "sess.jsonl"
    jsonl.write_text(
        json.dumps({"type": "file-history-snapshot"})
        + "\n"
        + json.dumps({"type": "user", "cwd": str(claude_ws)})
        + "\n",
        encoding="utf-8",
    )
    claude_ref = _Ref("claude_code:sess", 1.0, claude_ws.name, locator=str(jsonl))
    assert workspace_service._workspace_dir_for(claude_ref) == str(claude_ws)

    # Unrecoverable: unknown provider, DB ref with no session, missing stores.
    assert workspace_service._workspace_dir_for(_Ref("mystery:x", 1.0, "somewhere")) is None
    assert (
        workspace_service._workspace_dir_for(
            _Ref("opencode:x", 1.0, "somewhere", database=str(db_file))
        )
        is None
    )
    assert (
        workspace_service._workspace_dir_for(
            _Ref("kiro_ide_v2:x", 1.0, "somewhere", locator=str(tmp_path / "nope"))
        )
        is None
    )


def test_suggest_lists_dir_recovered_from_provider_store(monkeypatch, tmp_path: Path):
    """A ref the old code dropped now appears: its ``project`` is only the bare
    basename the real providers store, and the absolute dir is recovered from
    the provider store (plan D1/D2, Q4)."""
    ws_dir = tmp_path / "brick-lobster"
    ws_dir.mkdir()
    session_dir = tmp_path / "v2-session"
    session_dir.mkdir()
    (session_dir / "session.json").write_text(
        json.dumps({"workspacePaths": [str(ws_dir)]}), encoding="utf-8"
    )
    # project is the basename only — Path("brick-lobster").is_dir() is False,
    # so the old _candidate_dirs dropped this ref.
    refs = [_Ref("kiro_ide_v2:sess_x", 300.0, ws_dir.name, locator=str(session_dir))]
    _stub_chat_history(monkeypatch, refs, {})

    with _client() as client:
        resp = client.get("/workspaces/suggest")

    assert resp.status_code == 200
    assert str(ws_dir) in resp.text


# --- step 11e: add-workspace + control -----------------------------------------


def test_suggest_renders_datalist_options_with_ready_state(
    monkeypatch, tmp_path: Path
):
    """``/workspaces/suggest`` emits datalist-consumable ``<option>`` rows,
    tagging each dir ready (has ``.backseat/``) or needs-init (plan Step C/F2,
    Q6)."""
    ready = tmp_path / "ready-ws"
    (ready / ".backseat").mkdir(parents=True)
    plain = tmp_path / "plain-ws"
    plain.mkdir()
    refs = [
        _Ref("opencode:ses_ready", mtime=300.0, project=str(ready)),
        _Ref("opencode:ses_plain", mtime=200.0, project=str(plain)),
    ]
    _stub_chat_history(monkeypatch, refs, {})

    with _client() as client:
        resp = client.get("/workspaces/suggest")

    assert resp.status_code == 200
    body = resp.text
    ready_s = workspace_service._normalize(ready)
    plain_s = workspace_service._normalize(plain)
    assert f'<option value="{ready_s}">{ready_s} \u00b7 ready</option>' in body
    assert f'<option value="{plain_s}">{plain_s} \u00b7 needs init</option>' in body
    assert "hx-post" not in body


def test_homepage_has_add_workspace_plus_control():
    """The ``+`` disclosure, its path input, and its ``<datalist>`` live on the
    homepage but outside the 60s-polled ``#workspace-list`` fragment, so a poll
    can never wipe an open panel (plan Step C, F5/F8)."""
    with _client() as client:
        page = client.get("/")
        fragment = client.get("/workspaces/fragment")

    assert page.status_code == 200
    assert fragment.status_code == 200

    # The ``+`` control and its datalist-fed path input.
    assert 'class="expander"' in page.text
    assert 'class="expander-plus"' in page.text
    assert ">+</summary>" in page.text
    assert 'name="path"' in page.text
    assert 'list="ws-suggestions"' in page.text
    assert '<datalist id="ws-suggestions"' in page.text
    assert 'hx-get="/workspaces/suggest"' in page.text
    # The list refreshes immediately after a successful add (F8).
    assert "workspaceAdded from:body" in page.text

    # The polled fragment is cards only: no panel to lose on the outerHTML swap.
    assert "add-workspace" not in fragment.text
    assert 'name="path"' not in fragment.text
    assert "<datalist" not in fragment.text
    assert "workspaceAdded from:body" in fragment.text


def test_add_workspace_panel_is_bounded_and_holds_plus_and_form():
    """Owner brief: expanded, the ``+`` becomes a bounded panel (not the page
    width) with the ``+`` at its top-left and the path input + Add button
    inside it. Hermetic proxy for the geometry: the served CSS caps the panel
    width, and the markup nests both the ``+`` and the form in the panel."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    # The open state is the panel, capped well under the page width.
    assert ".expander[open]" in page.text
    assert "max-width: 100%" in page.text
    assert "width: 26rem" in page.text
    # The "+" and the path form are both descendants of the panel element.
    start = page.text.index('id="add-workspace"')
    panel = page.text[start : page.text.index("</details>", start)]
    assert 'class="expander-plus"' in panel
    assert 'name="path"' in panel
    assert 'type="submit"' in panel


def test_add_workspace_initializes_plain_dir_and_triggers_refresh(
    monkeypatch, tmp_path: Path, fake_home: Path
):
    """Adding an existing dir with no ``.backseat/`` creates the marker, then
    registers it and asks the list to refresh (plan Step C, F1/F8)."""
    plain = tmp_path / "plain-ws"
    plain.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    with _client() as client:
        resp = client.post("/workspaces", data={"path": str(plain)})

    assert resp.status_code == 200, resp.text
    assert (plain / ".backseat").is_dir()
    assert {p.name for p in plain.iterdir()} == {".backseat"}
    assert "Initialized and registered workspace" in resp.text
    assert resp.headers["HX-Trigger"] == "workspaceAdded"
    with db.open_db() as conn:
        assert str(plain.resolve()) in db.list_workspaces(conn)


def test_add_workspace_plus_css_centers_glyph():
    """The served CSS must keep the ``+`` centered in its round control
    (goal G2 / plan 3b): Pico pads ``summary`` vertically, which squeezes the
    2.1rem flex box asymmetrically once the box-sizing border-box rule
    applies, so the summary needs ``padding: 0``; ``place-items`` mirrors the
    explicit ``align-items``/``justify-content`` pair."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    css = page.text
    start = css.index(".expander-plus {")
    rule = css[start : css.index("}", start)]
    assert "display: inline-flex" in rule
    assert "place-items: center" in rule
    assert "align-items: center" in rule
    assert "justify-content: center" in rule
    assert "line-height: 1" in rule
    assert "list-style: none" in rule
    assert "padding: 0" in rule
    # Belt-and-braces: the UA's webkit disclosure marker stays suppressed.
    assert ".expander-plus::-webkit-details-marker" in css
    assert ".expander-plus::after" in css


def test_base_has_no_global_h1_and_smaller_font():
    """Goal G4: the redundant site-title header is gone and the base font is
    14px (plan 3c)."""
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "backseat-harness</h1>" not in resp.text
    assert 'class="nav-back"' not in resp.text
    body_start = resp.text.index("body {")
    body_css = resp.text[body_start : resp.text.index("}", body_start)]
    assert "font-size: 14px" in body_css


def test_workspace_page_back_nav_and_add_task_expander_after_active(
    workspace: Path,
):
    """The workspace page gets a back-link to ``/`` and the add-task form
    becomes a ``+`` expander placed after the (incomplete/Active) tasks table,
    still POSTing to ``/ws/{id}/tasks`` (goal G4 / plan 3c)."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    page = resp.text
    assert '<p class="nav-back"><a href="/">← Workspaces</a></p>' in page

    # Form moved into the expander, after the incomplete (Active) table.
    start = page.index('id="add-task"')
    panel = page[start : page.index("</details>", start)]
    assert 'class="expander add-task"' in page
    assert 'class="expander-plus"' in panel
    assert ">+</summary>" in panel
    assert f'action="/ws/{ws_id}/tasks"' in panel
    assert 'name="name"' in panel
    assert 'name="goal"' in panel
    # No "Add Task" heading section remains above/below the table.
    assert "<h3>Add Task</h3>" not in page
    active_table_end = page.index("</table>", page.index("Active tasks"))
    assert start > active_table_end
    assert start < page.index("Completed tasks")


def test_task_page_links_back_to_workspace(workspace: Path):
    """The task page gets a back-link to its workspace page (goal G4)."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "my-lane", updated_at=1_700_000_000.0)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}/tasks/my-lane")

    assert resp.status_code == 200
    assert f'<a href="/ws/{ws_id}">← {workspace.name}</a>' in resp.text


def test_badge_css_covers_all_and_unknown_status(workspace: Path):
    """Goal G5 / plan 3d: the served CSS colors complete green, continue amber
    and blocked red via ``data-status``; a lane with an unknown status value
    keeps the generic badge text and the grey default (no matching rule), and
    the page still renders. The task page uses the same ``data-status``
    mechanism."""
    with _client() as client:
        page = client.get("/")
        ws_id = _register_workspace(workspace)
        _write_lane(
            workspace, "weird-lane", status="weird", updated_at=1_700_000_000.0
        )
        ws_page = client.get(f"/ws/{ws_id}")
        task_page = client.get(f"/ws/{ws_id}/tasks/weird-lane")

    assert page.status_code == 200
    css = page.text
    assert '.badge[data-status="complete"]' in css
    assert '.badge[data-status="continue"]' in css
    assert '.badge[data-status="blocked"]' in css
    # No color rule for anything else: unknown falls to the grey default.
    assert '.badge[data-status="weird"]' not in css

    assert ws_page.status_code == 200
    assert 'data-status="weird"' in ws_page.text
    assert '<span class="badge" data-status="weird">in-progress</span>' in ws_page.text

    assert task_page.status_code == 200
    assert '<span class="badge" data-status="weird">in-progress</span>' in task_page.text


def test_add_workspace_registers_existing_workspace(
    monkeypatch, tmp_path: Path, fake_home: Path
):
    """Adding a dir that already has ``.backseat/`` registers it as before, with
    the immediate-refresh header and no initialization message (plan Q7)."""
    ws = tmp_path / "already-ws"
    (ws / ".backseat").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    with _client() as client:
        resp = client.post("/workspaces", data={"path": str(ws)})

    assert resp.status_code == 200, resp.text
    assert "Registered workspace" in resp.text
    assert "Initialized" not in resp.text
    assert resp.headers["HX-Trigger"] == "workspaceAdded"


def test_add_workspace_nonexistent_path_is_400(tmp_path: Path):
    """A path that is not a directory is refused (HTTP 400), and no refresh is
    triggered (plan Q7)."""
    missing = tmp_path / "not-here"
    with _client() as client:
        resp = client.post("/workspaces", data={"path": str(missing)})

    assert resp.status_code == 400
    assert "HX-Trigger" not in resp.headers


def test_add_task_duplicate_name_is_400(workspace: Path):
    """Adding a lane whose slug already exists is refused (400) and the first
    lane's goal is left untouched (2-plan §B edge case: duplicate add-task
    name)."""
    ws_id = _register_workspace(workspace)

    first = _post_task(ws_id, "Dup Lane", "first goal")
    assert first.status_code == 303

    second = _post_task(ws_id, "Dup Lane", "second goal")
    assert second.status_code == 400
    assert "already exists" in second.text

    goal = workspace / ".backseat" / "tasks" / "dup-lane" / "goal.md"
    assert goal.read_text(encoding="utf-8") == "first goal\n"


def test_unknown_workspace_and_task_ids_are_404(workspace: Path):
    """Unknown registry ids, unknown task slugs and POSTs to unknown
    workspaces all 404 rather than crashing (2-plan §B edge cases: nonexistent
    ws_id)."""
    ws_id = _register_workspace(workspace)
    bogus_id = ws_id + 9999

    with _client() as client:
        assert client.get(f"/ws/{bogus_id}").status_code == 404
        assert (
            client.get(f"/ws/{bogus_id}/tasks/anything").status_code == 404
        )
        # A real workspace with an unknown slug is also a 404.
        assert client.get(f"/ws/{ws_id}/tasks/no-such-lane").status_code == 404
        assert (
            client.post(
                f"/ws/{bogus_id}/tasks",
                data={"name": "x", "goal": "y"},
            ).status_code
            == 404
        )


# --- step 11b: workspace page listings -----------------------------------------


def _write_lane(
    workspace: Path,
    slug: str,
    *,
    updated_at: float,
    status: str | None = None,
    reason: str = "",
) -> None:
    """Create a task lane. ``status=None`` means no state.json, so the lane
    reads as ``continue`` ordered by goal.md's mtime."""
    lane = workspace / ".backseat" / "tasks" / slug
    lane.mkdir(parents=True, exist_ok=True)
    goal = lane / "goal.md"
    goal.write_text(f"goal for {slug}\n", encoding="utf-8")
    if status is None:
        os.utime(goal, (updated_at, updated_at))
    else:
        (lane / "state.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "status": status,
                    "iteration": 0,
                    "generation": 1,
                    "reason": reason,
                    "updated_at": updated_at,
                }
            ),
            encoding="utf-8",
        )


def _section(html: str, heading: str) -> str:
    """Body of the ``<section>`` that starts at ``heading``."""
    start = html.index(heading)
    return html[start : html.index("</section>", start)]


def _section(html: str, heading: str) -> str:
    """Body of the ``<section>`` that starts at ``heading``."""
    start = html.index(heading)
    return html[start : html.index("</section>", start)]


def _other_chats_section(html: str) -> str:
    """Body of the collapsed ``<details id="other-chats">`` block."""
    start = html.index('<details id="other-chats">')
    return html[start : html.index("</details>", start)]


def test_workspace_page_sections_tasks_status_and_chat_filtering(
    monkeypatch, workspace: Path
):
    ws_id = _register_workspace(workspace)

    # Status lanes: blocked -> "needs attention", continue -> "in-progress",
    # complete -> dropped from Active but listed under Completed tasks.
    _write_lane(
        workspace,
        "blocked-lane",
        status="blocked",
        reason="Pick a palette color",
        updated_at=1_700_000_300.0,
    )
    _write_lane(
        workspace, "continue-lane", status="continue", updated_at=1_700_000_100.0
    )
    _write_lane(
        workspace, "done-lane", status="complete", updated_at=1_700_000_200.0
    )
    # 22 continue lanes (no state.json) so the 10-cap on Recent tasks is
    # exercised; their goal mtimes keep them below the three status lanes.
    for i in range(22):
        _write_lane(
            workspace, f"filler-{i:02d}", status=None, updated_at=1_700_000_000.0 + i
        )

    other = workspace.parent / "some-other-project"
    other.mkdir()
    refs = [
        _Ref("opencode:ses_ws_new", mtime=300.0, project=str(workspace)),
        _Ref("opencode:ses_ws_marker", mtime=400.0, project=str(workspace)),
        _Ref("claude_code:ses_ws_old", mtime=200.0, project=str(workspace)),
        _Ref("opencode:ses_elsewhere", mtime=500.0, project=str(other)),
    ]
    transcripts = {
        "opencode:ses_ws_new": "chatting about widgets",
        "opencode:ses_ws_marker": f"managed {_MARKER}",
        "claude_code:ses_ws_old": "chatting about gadgets",
        "opencode:ses_elsewhere": "other project chatter",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")
    assert resp.status_code == 200

    active = _section(resp.text, "<h3>Active tasks</h3>")
    completed = _section(resp.text, "<h3>Completed tasks</h3>")
    recent = _section(resp.text, "<h3>Recent tasks</h3>")
    chats = _section(resp.text, "<h3>Recent chats</h3>")

    # The goal's order: Active -> (add-task expander) -> Completed -> Recent
    # tasks -> Recent chats -> Other chats (collapsed details).
    heading_positions = [
        resp.text.index(h)
        for h in ("<h3>Active tasks</h3>", "<h3>Completed tasks</h3>",
                  "<h3>Recent tasks</h3>", "<h3>Recent chats</h3>",
                  '<details id="other-chats">')
    ]
    assert heading_positions == sorted(heading_positions)
    assert resp.text.index("id=\"add-task\"") > resp.text.index("</table>", heading_positions[0])

    # Active: badge text per status, complete lane excluded, uncapped
    # (still lists every incomplete lane, now 24).
    inc_rows = _data_rows(active)
    assert len(inc_rows) == 24  # 25 lanes minus the complete one
    blocked_row = next(r for r in inc_rows if ">blocked-lane<" in r)
    assert '<span class="badge" data-status="blocked">needs attention</span>' in blocked_row
    continue_row = next(r for r in inc_rows if ">continue-lane<" in r)
    assert '<span class="badge" data-status="continue">in-progress</span>' in continue_row
    assert all("done-lane" not in r for r in inc_rows)
    assert ">filler-00<" in active  # oldest active lane still listed

    # Completed tasks: only the complete lane.
    comp_rows = _data_rows(completed)
    assert len(comp_rows) == 1
    assert ">done-lane<" in comp_rows[0]
    assert '<span class="badge" data-status="complete">complete</span>' in comp_rows[0]

    # Recent tasks: capped at 10, newest first, complete included.
    rec_rows = _data_rows(recent)
    assert len(rec_rows) == 10
    assert ">blocked-lane<" in rec_rows[0]
    assert ">done-lane<" in recent
    assert ">filler-21<" in recent
    assert ">filler-15<" in recent  # the 10th row, right on the cap
    assert ">filler-14<" not in recent  # the eleventh falls off
    assert ">filler-00<" not in recent

    # Recent chats: workspace-scoped, <meta> marker sessions excluded,
    # newest first, Prompt column linking the chat page.
    chat_rows = _data_rows(chats)
    assert len(chat_rows) == 2
    assert "chatting about widgets" in chat_rows[0]
    assert 'href="/chat/opencode/ses_ws_new?ws=1"' in chat_rows[0]
    assert "chatting about gadgets" in chat_rows[1]
    assert 'href="/chat/claude_code/ses_ws_old?ws=1"' in chat_rows[1]
    assert "ses_ws_marker" not in resp.text
    assert "managed" not in resp.text
    assert "ses_elsewhere" not in resp.text
    assert "other project chatter" not in resp.text
    # No session-id column: the native ids render only inside the hrefs.
    assert "ses_ws_new" in chat_rows[0].split("<td>")[1]  # inside the link only
    assert "<th>Session</th>" not in resp.text

    # Other chats: exists as a closed cohort for this population too.
    other = _other_chats_section(resp.text)
    assert "No other chats." in other
    assert _data_rows(other) == []


def test_workspace_page_splits_recent_and_other_chats(
    monkeypatch, workspace: Path
):
    """12 clean workspace chats + 1 marker chat: the ten newest land under
    "Recent chats", the rest inside a closed "Other chats" ``<details>``; the
    marker session appears in neither (goal G6 / plan 3e)."""
    ws_id = _register_workspace(workspace)
    refs = []
    transcripts = {}
    for i in range(12):
        key = f"opencode:ses_split_{i:02d}"
        refs.append(_Ref(key, mtime=100.0 + i, project=str(workspace)))
        transcripts[key] = f"## USER\nchat {i:02d} about widgets"
    marker_key = "claude_code:ses_split_marker"
    refs.append(_Ref(marker_key, mtime=999.0, project=str(workspace)))
    transcripts[marker_key] = f"managed {_MARKER}"
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    recent = _section(resp.text, "<h3>Recent chats</h3>")
    other = _other_chats_section(resp.text)
    recent_rows = _data_rows(recent)
    other_rows = _data_rows(other)

    assert len(recent_rows) == 10
    assert len(other_rows) == 2
    # Newest ten first (ses_split_11 down to ses_split_02).
    assert 'href="/chat/opencode/ses_split_11?ws=1"' in recent_rows[0]
    assert 'href="/chat/opencode/ses_split_02?ws=1"' in recent_rows[-1]
    # ...and the other two are the oldest.
    assert 'href="/chat/opencode/ses_split_01?ws=1"' in other_rows[0]
    assert 'href="/chat/opencode/ses_split_00?ws=1"' in other_rows[1]
    # Marker sessions are excluded from both sections.
    assert "ses_split_marker" not in recent
    assert "ses_split_marker" not in other
    # The details block has no ``open`` attribute: collapsed by default.
    tag_start = resp.text.index('<details id="other-chats"')
    tag = resp.text[tag_start : resp.text.index(">", tag_start) + 1]
    assert tag == '<details id="other-chats">'
    # Both sections share the Prompt/Updated columns.
    assert "<th>Prompt</th>" in recent and "<th>Prompt</th>" in other
    assert "<th>Session</th>" not in resp.text


def test_completed_tasks_section_renders_when_empty(
    monkeypatch, workspace: Path
):
    """An empty Completed list renders the heading with the empty-state row
    and no data rows (plan edge case)."""
    _register_workspace(workspace)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        resp = client.get("/ws/1")

    assert resp.status_code == 200
    completed = _section(resp.text, "<h3>Completed tasks</h3>")
    assert "No completed tasks." in completed
    assert _data_rows(completed) == []


def test_prompt_excerpt_normalizes_and_truncates():
    """The prompt string: leading <meta> markers dropped, first ``## USER``
    turn body preferred, newlines/tabs collapsed to spaces, hard cut at 120
    chars, whole-text fallback without headers (plan 3e / Q4)."""
    excerpt = chat_search_service._prompt_excerpt

    assert excerpt("## USER\nHello\nworld\ttabbed") == "Hello world tabbed"
    assert (
        excerpt('<meta>START SESSION 123-abcd #1</meta>\n## USER\nhi there')
        == "hi there"
    )
    assert (
        excerpt("## USER\nthe prompt\n## ASSISTANT\nan assistant reply")
        == "the prompt"
    )
    assert excerpt("## USER\n" + "x" * 200) == "x" * 120
    assert excerpt("plain text\n\twith lines") == "plain text with lines"
    assert excerpt("") == ""


def test_workspace_page_reads_transcripts_in_one_batch(monkeypatch, workspace: Path):
    """Regression guard for the slow ``/ws/<id>``: one batched transcript read.

    The pre-fix path asked chat-mother-forker for each row's transcript one at
    a time, and every one of those tiered searches re-parses *every* stored
    conversation of *every* provider -- measured at 2.4s per row against this
    machine's 344 stored conversations, 147s end-to-end for one workspace
    page. The page must ask once, with all candidate keys at once, and it
    must hand the fetch the *same* memoized providers the search used: that
    sharing is what keeps the whole render at one parse per row instead of
    one per pass.
    """
    ws_id = _register_workspace(workspace)
    # 11 chats split across the two sections on next render: the ten newest
    # feed "Recent chats", the oldest one only "Other chats" -- and the single
    # batch must still cover all of them at once.
    refs = []
    transcripts = {}
    for key, mtime, text in [
        ("opencode:ses_batch_a", 300.0, "chat about widgets"),
        ("claude_code:ses_batch_b", 200.0, "chat about gadgets"),
    ] + [
        (f"opencode:ses_batch_c{i:02d}", 190.0 - i, f"filler chat {i:02d}")
        for i in range(9)
    ]:
        refs.append(_Ref(key, mtime=mtime, project=str(workspace)))
        transcripts[key] = text
    _stub_chat_history(monkeypatch, refs, transcripts)

    from backseat_harness import history

    calls: list[list[str]] = []
    seen: dict[str, object] = {}
    batched = history.load_sessions

    # Record the provider list the search was handed, so the transcript fetch
    # can be checked for passing the very same one.
    real_load = history._load

    def _spy_load(attr: str):
        impl = real_load(attr)
        if attr != "find_chats":
            return impl

        def _find_chats(query="", providers=None, limit=None):
            seen["search_providers"] = providers
            return impl(query, providers=providers, limit=limit)

        return _find_chats

    def _spy(keys, providers=None) -> dict[str, str]:
        requested = list(keys)
        calls.append(requested)
        seen["load_providers"] = providers
        return batched(requested, providers=providers)

    monkeypatch.setattr(history, "_load", _spy_load)
    monkeypatch.setattr(history, "load_sessions", _spy)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    assert len(calls) == 1, f"transcripts were fetched {len(calls)} times, not once"
    expected_keys = {r.key for r in refs}
    assert set(calls[0]) == expected_keys
    assert seen["load_providers"] is seen["search_providers"]
    # The one batch covers rows rendered in both chat sections.
    assert "chat about widgets" in page.text
    assert "chat about gadgets" in page.text
    other = _other_chats_section(page.text)
    assert "filler chat 08" in other  # the oldest-only key
    assert "filler chat 08" not in _section(page.text, "<h3>Recent chats</h3>")


# --- step 11c: task viewer Run and Takeover controls ---------------------------


class _InputParser(HTMLParser):
    """Collect input name -> value the way a browser's form parser would."""

    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "input":
            d = dict(attrs)
            if "name" in d:
                self.inputs[d["name"]] = d.get("value")


def _hidden_input(html: str, name: str) -> str:
    """Strict attribute parse of one hidden input (raises if absent/malformed)."""
    parser = _InputParser()
    parser.feed(html)
    return parser.inputs[name]


_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(data: bytes) -> bytes:
    """Drop terminal control sequences, as a terminal renders them away."""
    return _ANSI_RE.sub(b"", data)


def _session_buffer(session_id: str) -> bytes:
    session = _shell_manager().get_session(session_id)
    if session is None:
        return b""
    with session.lock:
        return b"".join(session.buffer)


def _wait_stripped(session_id: str, needle: bytes, timeout: float = 30.0) -> bytes:
    """Poll the ring buffer (ANSI-stripped) until ``needle`` lands in it."""
    deadline = time.monotonic() + timeout
    snapshot = b""
    while time.monotonic() < deadline:
        snapshot = _strip_ansi(_session_buffer(session_id))
        if needle in snapshot:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never reached the PTY; buffer={snapshot!r}")


def _wait_exit(session_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    manager = _shell_manager()
    while time.monotonic() < deadline:
        if not manager.is_alive(session_id):
            return
        time.sleep(0.05)
    raise AssertionError("the shell's child process never exited")


def test_task_viewer_run_button_invokes_cli_with_brief(monkeypatch, workspace: Path):
    ws_id = _register_workspace(workspace)
    assert _post_task(ws_id, "Viewer Task", "ship the thing").status_code == 303
    slug = "viewer-task"
    ws_arg = str(workspace.resolve())

    # Process-boundary stub: record the exact argv the CLI would be spawned with.
    spawned: list[tuple[list[str], dict]] = []

    def _record_popen(cmd, **kwargs):  # noqa: ANN001
        spawned.append((cmd, kwargs))
        return object()

    monkeypatch.setattr(subprocess, "Popen", _record_popen)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/{slug}")
        assert page.status_code == 200
        assert f'action="/ws/{ws_id}/tasks/{slug}/run"' in page.text
        assert ">Run task<" in page.text

        with_brief = client.post(
            f"/ws/{ws_id}/tasks/{slug}/run",
            data={"brief": "also fix the flaky test"},
            follow_redirects=False,
        )
        assert with_brief.status_code == 303
        assert with_brief.headers["location"] == f"/ws/{ws_id}/tasks/{slug}"

        no_brief = client.post(
            f"/ws/{ws_id}/tasks/{slug}/run", follow_redirects=False
        )
        assert no_brief.status_code == 303

    assert len(spawned) == 2
    cmd, kwargs = spawned[0]
    assert cmd == [
        sys.executable,
        "-m",
        "backseat_harness.cli",
        "-C",
        ws_arg,
        "run",
        slug,
        "also fix the flaky test",
    ]
    assert kwargs["cwd"] == ws_arg
    # Without a brief the argv stops at the slug.
    assert spawned[1][0] == [
        sys.executable, "-m", "backseat_harness.cli", "-C", ws_arg, "run", slug
    ]


def test_task_viewer_takeover_starts_shell_running_takeover_cli(
    monkeypatch, workspace: Path, fake_home: Path
):
    ws_id = _register_workspace(workspace)
    assert _post_task(ws_id, "Takeover Lane", "needs a decision").status_code == 303
    slug = "takeover-lane"
    ws_arg = str(workspace.resolve())

    # The button spawns the real CLI, so make that run hermetic and
    # deterministic: the child inherits an isolated home (empty registry ->
    # nothing to kill) and a workspace config whose provider cannot resolve,
    # which stops the takeover before any interactive TUI could start.
    (workspace / ".backseat" / "config.toml").write_text(
        '[harness]\nprovider = "definitely-not-installed"\n', encoding="utf-8"
    )
    (workspace / ".backseat" / "tasks" / slug / "state.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "status": "blocked",
                "iteration": 0,
                "generation": 1,
                "reason": "Pick a palette color",
                "updated_at": 1_700_000_300.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/{slug}")
        assert page.status_code == 200
        assert ">Takeover<" in page.text

        # The button's payload is browser-parseable JSON of the exact argv.
        argv = json.loads(_hidden_input(page.text, "cmd"))
        assert argv == [
            sys.executable,
            "-m",
            "backseat_harness.cli",
            "-C",
            ws_arg,
            "takeover",
            slug,
        ]
        assert _hidden_input(page.text, "cwd") == "."

        session_id = _start_session(client, argv, Path("."))
        try:
            assert client.get(f"/shell/{session_id}").status_code == 200

            # Real CLI output: it loaded this lane and reported the block.
            out = _wait_stripped(
                session_id,
                b"This lane is blocked on a question from the operator:",
            )
            assert b"Pick a palette color" in out

            # ...and then failed fast at provider resolution, before any
            # interactive handoff could happen.
            _wait_stripped(session_id, b"unknown provider")
            _wait_exit(session_id)
            final = _strip_ansi(_session_buffer(session_id))
            assert b"definitely-not-installed" in final
            assert b"Handing you" not in final
        finally:
            _kill_session(session_id)


# --- step 11d: chat viewer resume + fork prompt --------------------------------


def test_chat_viewer_renders_resume_argv_for_native_cli(monkeypatch):
    from backseat_harness import history

    transcript = "User: fix the flaky test\nAssistant: done, the test is stable"
    # The viewer asks for the exact key in the URL (one parse), not a search.
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: transcript for k in keys}
    )

    with _client() as client:
        resp = client.get("/chat/opencode/ses_resume")

    assert resp.status_code == 200
    assert "fix the flaky test" in resp.text
    assert ">Continue<" in resp.text
    # Browser-parseable argv for the web-shell resume.
    assert json.loads(_hidden_input(resp.text, "cmd")) == [
        "opencode",
        "--session",
        "ses_resume",
    ]
    assert _hidden_input(resp.text, "cwd") == "."

    # A provider with no interactive resume gets the transcript and a fork
    # button (posts to /search/fork) instead of the continue form.
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide-side chat" for k in keys},
    )
    with _client() as client:
        resp2 = client.get("/chat/kiro_ide/sess_x")
    assert resp2.status_code == 200
    assert "ide-side chat" in resp2.text
    assert "Resume in Shell" not in resp2.text
    assert 'action="/search/fork"' in resp2.text
    assert _hidden_input(resp2.text, "conversation_id") == "sess_x"
    assert _hidden_input(resp2.text, "current_session_key") == "kiro_ide:sess_x"


def test_chat_page_sticky_header_shows_prompt_and_continue(monkeypatch):
    from backseat_harness import history

    # Newlines collapse to spaces in the header prompt (same helper as the
    # 3e lists), and the full transcript body is not truncated.
    tail = "TRANSCRIPT-GOES-ON-" + "long body chunk " * 40
    transcript = (
        "<meta>START SESSION x</meta>\n"
        "## USER\nfix the\tflaky test now\n"
        "## ASSISTANT\ndone\n" + tail
    )
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: transcript for k in keys}
    )

    with _client() as client:
        resp = client.get("/chat/opencode/ses_header")

    assert resp.status_code == 200
    # The sticky header holds the same normalized prompt string the lists show.
    assert 'class="chat-header"' in resp.text
    assert "fix the flaky test now" in resp.text
    assert "<meta>" not in resp.text
    # Header CSS: position sticky, top 0 (base.html style block).
    assert "position: sticky;\n      top: 0;" in resp.text
    # The continue button is the resume form's submit control.
    assert ">Continue<" in resp.text
    assert json.loads(_hidden_input(resp.text, "cmd")) == [
        "opencode",
        "--session",
        "ses_header",
    ]
    # The transcript body is full: the far tail survives.
    assert "TRANSCRIPT-GOES-ON-" in resp.text
    assert resp.text.count("long body chunk") == 40


def test_chat_page_falls_back_to_fork_without_resume_command(monkeypatch):
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )
    with _client() as client:
        resp = client.get("/chat/cline/sess_fork")

    assert resp.status_code == 200
    assert "ide chat body" in resp.text
    # No continue form for a provider without an interactive resume; instead
    # a fork form posting conversation_id + current_session_key to /search/fork.
    assert 'id="resume-form"' not in resp.text
    assert ">Continue<" not in resp.text
    assert 'action="/search/fork"' in resp.text
    assert _hidden_input(resp.text, "conversation_id") == "sess_fork"
    assert _hidden_input(resp.text, "current_session_key") == "cline:sess_fork"
    assert ">Fork<" in resp.text


def test_unknown_chat_provider_and_session_are_404(monkeypatch):
    """Unknown provider names and known providers with no transcript both 404
    (2-plan §B edge case: unknown chat / provider)."""
    from backseat_harness import history

    # A known provider whose loader finds nothing for the requested key.
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {}
    )

    with _client() as client:
        bad_provider = client.get("/chat/not_a_provider/ses_x")
        missing_session = client.get("/chat/opencode/ses_missing")

    assert bad_provider.status_code == 404
    assert "not_a_provider" in bad_provider.text
    assert missing_session.status_code == 404
    assert "not found" in missing_session.text.lower()


def test_search_fork_sends_fork_prompt_into_the_pty(monkeypatch, tmp_path: Path):
    # One unsupported-provider chat: pick_fork_provider skips the Kiro-IDE and
    # most-recent-supported rules and falls to the PATH scan, which we pin to a
    # fake "opencode" (rule 3 of the goal's fork-provider selection).
    refs = [_Ref("cursor:sess_cur", mtime=200.0, project=None)]
    _stub_chat_history(monkeypatch, refs, {"cursor:sess_cur": "cursor chat"})

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_opencode = fake_bin / "opencode.exe"
    shutil.copy2(sys.executable, fake_opencode)
    shutil.copy2(Path(sys.prefix) / "pyvenv.cfg", fake_bin / "pyvenv.cfg")

    def _which(command: str, path: str | None = None) -> str | None:
        # pick_fork_provider scans for the CLI, and pywinpty's spawn resolves
        # argv[0] through shutil.which too -- both land on the fake binary.
        return str(fake_opencode) if command == "opencode" else None

    monkeypatch.setattr("shutil.which", _which)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    with _client() as client:
        resp = client.post(
            "/search/fork",
            data={
                "conversation_id": "sess_cur",
                "current_session_key": "cursor:sess_cur",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert re.fullmatch(r"/shell/[0-9a-f]{12}", location)
        session_id = location.rsplit("/", 1)[1]
        try:
            assert client.get(f"/shell/{session_id}").status_code == 200
            # The route writes "Please fork chat id <id>\n" into the PTY 0.5s
            # after spawn; the fake TUI is a real child whose terminal renders
            # the line back, so the full prompt must land in the ring buffer.
            _wait_stripped(
                session_id, b"Please fork chat id sess_cur", timeout=45.0
            )
        finally:
            _kill_session(session_id)

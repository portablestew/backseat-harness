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
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backseat_harness import db, marker
from backseat_harness.web.main import create_app
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import task as task_service
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


def _home_row(html: str, ws_id: int) -> str:
    """The homepage ``<tr>`` for ``ws_id`` (row navigation via ``data-href``)."""
    marker = html.index(f'data-href="/ws/{ws_id}"')
    start = html.rindex("<tr", 0, marker)
    return html[start : html.index("</tr>", marker) + len("</tr>")]


def _css_rule_bodies(html: str) -> dict[str, list[str]]:
    """Declaration bodies of the page's ``<style>`` rules, keyed by selector."""
    rules: dict[str, list[str]] = {}
    for style in re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.S):
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
            rules.setdefault(" ".join(selector.split()), []).append(body)
    return rules


def _home_tasks_cell(html: str, ws_id: int) -> str:
    """Inner HTML of the "Tasks" ``<td>`` on that row (last cell)."""
    row = _home_row(html, ws_id)
    return re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)[-1]


def test_add_workspace_button_label_and_disabled_until_path():
    """The homepage add control reads "Add workspace", starts disabled, and a
    small inline script re-evaluates on every input event, so a path that is
    empty after trimming keeps it disabled (goal.md:7 / plan 3b)."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    body = page.text
    button = re.search(
        r"<button\b[^>]*id=\"add-workspace-submit\"[^>]*>(.*?)</button>",
        body,
        re.S,
    )
    assert button is not None
    assert button.group(1).strip() == "Add workspace"
    assert "disabled" in button.group(0)
    assert ">Add<" not in body
    # The enabling JS is keyed off the path input and listens for input events.
    assert "getElementById('ws-path')" in body
    assert "getElementById('add-workspace-submit')" in body
    assert "addEventListener('input'" in body
    assert "input.value.trim().length === 0" in body


@pytest.mark.parametrize(
    ("count", "more_text"),
    [(0, None), (3, None), (4, "... and 1 more")],
)
def test_homepage_tasks_column_latest_three_plus_more(workspace, count, more_text):
    """The homepage Tasks column links up to three task pages and appends
    "... and N more"; a workspace with 0 tasks renders an empty cell. Page and
    polled fragment render the same cell (goal.md:25)."""
    slugs = ["lane-a", "lane-b", "lane-c", "lane-d"][:count]
    tasks_root = workspace / ".backseat" / "tasks"
    for i, slug in enumerate(slugs):
        when = 1_700_000_000.0 + i
        _write_lane(workspace, slug, status="continue", updated_at=when)
        os.utime(tasks_root / slug, (when, when))
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get("/")
        fragment = client.get("/workspaces/fragment")

    assert page.status_code == 200
    assert fragment.status_code == 200
    for html in (page.text, fragment.text):
        cell = _home_tasks_cell(html, ws_id)
        links = re.findall(
            r'<a class="task-link" href="(/ws/\d+/tasks/[^"]+)">([^<]+)</a>', cell
        )
        assert len(links) == min(count, 3)
        if count == 0:
            assert cell.strip() == ""
        if more_text:
            assert more_text in cell
        else:
            assert "more" not in cell


def test_homepage_tasks_cell_is_comma_separated(workspace):
    """The Tasks cell reads as plain text ``a, b, c, ... and N more``: task
    links are joined by commas and the "... and N more" tail follows a comma.
    The page and polled fragment render the identical cell (goal.md:25)."""
    tasks_root = workspace / ".backseat" / "tasks"
    lanes = {
        "newest": 1_700_000_300.0,
        "newer": 1_700_000_200.0,
        "older": 1_700_000_100.0,
        "oldest": 1_700_000_000.0,
    }
    for slug, when in lanes.items():
        _write_lane(workspace, slug, status="continue", updated_at=when)
        os.utime(tasks_root / slug, (when, when))
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get("/")
        fragment = client.get("/workspaces/fragment")

    cells = [_home_tasks_cell(h.text, ws_id) for h in (page, fragment)]
    assert cells[0] == cells[1]
    text = re.sub(r"<[^>]+>", "", cells[0])
    assert text == "newest, newer, older, ... and 1 more"


def test_homepage_tasks_column_orders_by_task_dir_mtime(workspace):
    """The Tasks column orders by each task directory's own ``st_mtime``,
    newest first, and drops all but the newest three -- set explicitly here
    with ``os.utime`` since the state's ``updated_at`` is a different clock
    (goal.md:25 / plan decision 3)."""
    lanes = {
        "oldest": 1_700_000_000.0,
        "older": 1_700_000_100.0,
        "newer": 1_700_000_200.0,
        "newest": 1_700_000_300.0,
    }
    tasks_root = workspace / ".backseat" / "tasks"
    for slug, when in lanes.items():
        _write_lane(workspace, slug, status="continue", updated_at=when)
        os.utime(tasks_root / slug, (when, when))
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    cell = _home_tasks_cell(page.text, ws_id)
    shown = re.findall(r'<a class="task-link" href="/ws/\d+/tasks/([^"]+)">', cell)
    assert shown == ["newest", "newer", "older"]
    assert "... and 1 more" in cell
    assert "oldest" not in cell


def test_homepage_task_links_inherit_row_hover_and_open_the_task(workspace):
    """The Tasks-column links sit inside the same ``.data-table`` row, so they
    inherit 3a's row hover + underline, and the delegated row-click handler
    bails on anchors, so the link's own task URL wins over the row's workspace
    URL (goal.md:9-11, 25)."""
    _write_lane(workspace, "lane-a", status="continue", updated_at=1_700_000_000.0)
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get("/")

    body = page.text
    row = _home_row(body, ws_id)
    assert f'<a class="task-link" href="/ws/{ws_id}/tasks/lane-a"' in row
    assert f'data-href="/ws/{ws_id}"' in row
    # 3a: only populated rows highlight.
    assert "tbody tr:not(.empty-row):hover" in body
    # Task links underline under their own pointer; the row's workspace link
    # underlines on row hover except while a task link is hovered.
    assert ".data-table a.task-link:hover" in body
    assert "a:not(.task-link)" in body
    assert ":not(:has(a.task-link:hover))" in body
    # The click handler bails on anchors, so the task link's href wins.
    assert "closest('a, button, form, input, textarea, select')" in body


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

# The root UUID inside ``_MARKER``, as a lane's ``binding.json`` stores it.
_MARKER_ROOT_UUID = marker.parse_marker(_MARKER)[0]


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
    - ``find_chats`` (the parse-per-row search) for the search page,
    - ``list_session_refs`` (the cheap, unparsed listing) for the workspace
      page's chat window, the suggestion list and the fork route,
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
    """Body rows of a table (header rows use <th>, not <td>); rows may carry
    attributes such as the shared style's ``data-href``."""
    return [
        row
        for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.S)
        if "<td>" in row
    ]


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


def test_shared_store_is_read_once_per_page(tmp_path: Path, monkeypatch):
    """Many ids from one SQLite store cost one read, not one query per id.

    A workspace page probes up to a hundred opencode/kilo ids, and the per-id
    ``SELECT`` re-scans (and re-opens) the table every time -- 0.8s across
    opencode's store alone, the cost this guard exists to prevent.
    """
    db_file = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE session (id TEXT, directory TEXT)")
    conn.executemany(
        "INSERT INTO session VALUES (?, ?)",
        [(f"ses_{i}", str(tmp_path / f"ws-{i}")) for i in range(50)],
    )
    conn.commit()
    conn.close()

    real_connect = sqlite3.connect
    connects: list[object] = []

    def _spy(*args, **kwargs):
        connects.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(workspace_service.sqlite3, "connect", _spy)

    assert (
        workspace_service._dir_from_session_db(str(db_file), "ses_3")
        == str(tmp_path / "ws-3")
    )
    assert (
        workspace_service._dir_from_session_db(str(db_file), "ses_47")
        == str(tmp_path / "ws-47")
    )
    assert workspace_service._dir_from_session_db(str(db_file), "ses_missing") is None
    assert len(connects) == 1, f"shared store opened {len(connects)} times, not once"


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

    # The round control and its datalist-fed path input. The glyph is drawn by
    # the stylesheet (two centered bars) so it centers exactly and becomes a
    # minus while the panel is open.
    assert 'class="expander"' in page.text
    assert 'class="expander-plus"' in page.text
    assert ".expander-plus::before" in page.text
    assert ">+</summary>" not in page.text
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
    # Smaller than before (owner brief).
    assert "width: 1.7rem" in rule
    assert "height: 1.7rem" in rule
    # The glyph is two bars pinned to the control's exact midpoint, so its
    # vertical centering does not depend on font metrics (owner brief: the
    # glyph sat slightly below center).
    bar_start = css.index(".expander-plus::before,")
    bar_rule = css[bar_start : css.index("}", bar_start)]
    assert "position: absolute" in bar_rule
    assert "top: 50%" in bar_rule
    assert "left: 50%" in bar_rule
    assert "translate(-50%, -50%)" in bar_rule
    # Belt-and-braces: the UA's webkit disclosure marker stays suppressed.
    assert ".expander-plus::-webkit-details-marker" in css
    assert ".expander-plus::after" in css
    # Expanded/hover no longer fill with the loud accent; they stay on the
    # surface palette, and the open state collapses the vertical bar (minus).
    hover_start = css.index(".expander-plus:hover")
    hover_rule = css[hover_start : css.index("}", hover_start)]
    assert "background: var(--surface-hover)" in hover_rule
    assert "--accent" not in hover_rule
    open_start = css.index(".expander[open] .expander-plus {")
    open_rule = css[open_start : css.index("}", open_start)]
    assert "background: var(--surface-hover)" in open_rule
    assert "scaleY(0)" in css


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


def test_toast_status_sits_at_the_bottom():
    """Owner brief: the add-workspace status ("ok: Initialized and registered
    workspace ...") is shown at the bottom of the page, so ``#toast`` renders
    after the content block rather than above it."""
    with _client() as client:
        home = client.get("/")

    assert home.status_code == 200
    assert 'id="toast"' in home.text
    assert home.text.index('id="toast"') > home.text.rindex("</section>")


def test_page_and_section_headers_are_one_level_smaller(
    monkeypatch, workspace: Path
):
    """Owner brief: page titles drop one heading level (h2 -> h3) and section
    headings drop one (h3 -> h4) across the site."""
    ws_id = _register_workspace(workspace)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        home = client.get("/")
        page = client.get(f"/ws/{ws_id}")

    assert "<h3>Workspaces</h3>" in home.text
    assert "<h2>Workspaces</h2>" not in home.text
    # The workspace page title is the chat-style header <strong>, not a heading.
    assert f"<h3>{workspace.name}</h3>" not in page.text
    assert "<h4>Active tasks</h4>" in page.text
    assert "<h4>Completed tasks</h4>" in page.text
    assert "<h4>Other chats</h4>" in page.text
    assert "<h3>Active tasks</h3>" not in page.text


def test_workspace_page_back_nav_and_add_task_expander_after_active(
    workspace: Path,
):
    """The workspace page gets exactly one back-link to ``/`` and the add-task
    form becomes a ``+`` expander placed after the (incomplete/Active) tasks
    table, still POSTing to ``/ws/{id}/tasks`` (goal G4 / plan 3c; the back link
    now rides the shared chat-style header)."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    page = resp.text
    assert '<a class="chat-header-back" href="/">← Workspaces</a>' in page
    # Exactly one: the link is not rendered twice (stacked).
    assert page.count('class="chat-header-back"') == 1

    # Form moved into the expander, after the incomplete (Active) table. The
    # glyph is drawn by the stylesheet, not a literal summary character.
    start = page.index('id="add-task"')
    panel = page[start : page.index("</details>", start)]
    assert 'class="expander add-task"' in page
    assert 'class="expander-plus"' in panel
    assert ">+</summary>" not in panel
    assert f'action="/ws/{ws_id}/tasks"' in panel
    assert 'name="name"' in panel
    assert 'name="goal"' in panel
    # No "Add Task" heading section remains above/below the table.
    assert "<h3>Add Task</h3>" not in page
    active_table_end = page.index("</table>", page.index("Active tasks"))
    assert start > active_table_end
    assert start < page.index("Completed tasks")


def test_workspace_header_matches_chat_style_without_actions(workspace: Path):
    """The workspace page reuses the shared chat-page header: one back link,
    workspace name as the top line over the workspace dir, and no fork or
    takeover control (goal.md:13, 27 / plan 3c)."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    html = resp.text
    assert '<header class="chat-header">' in html
    assert html.count('class="chat-header-back"') == 1
    assert '<a class="chat-header-back" href="/">← Workspaces</a>' in html

    header = html[html.index('class="chat-header">') : html.index("</header>")]
    name = f"<strong>{workspace.name}</strong>"
    path = f"<small>{workspace.resolve()}</small>"
    assert name in header
    assert path in header
    # Name is the top line; the workspace dir sits beneath it.
    assert header.index(name) < header.index(path)

    # No fork/takeover controls anywhere on this page.
    assert ">Fork<" not in html
    assert ">Takeover<" not in html
    assert "/search/fork" not in html
    assert 'class="chat-continue-form"' not in html


def test_workspace_page_auto_refreshes_every_60s(workspace: Path):
    """The workspace page opts into the shared 1-minute auto-refresh; the
    homepage keeps its own HTMX poll and does not carry the hook. The helper
    pauses while typing or an expander is open (goal.md:3 / plan 3c)."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")
        home = client.get("/")

    assert page.status_code == 200
    assert '<main class="container" data-auto-refresh="60">' in page.text
    # The shared helper lives in the base layout and times a reload.
    body = page.text
    assert "window.location.reload()" in body
    assert "details.expander[open]" in body
    # The script sits in <head>, before <main> is parsed, so it must wait for
    # DOMContentLoaded before querying the attribute -- otherwise root is null
    # and the page never refreshes.
    refresh_at = body.index("[data-auto-refresh]")
    script_start = body.rindex("<script>", 0, refresh_at)
    script = body[script_start : body.index("</script>", refresh_at)]
    dom_ready = script.index("document.addEventListener('DOMContentLoaded'")
    assert dom_ready < script.index("[data-auto-refresh]")
    assert dom_ready < script.index("window.location.reload()")
    # Homepage keeps the HTMX poll instead of the reload hook.
    assert home.status_code == 200
    assert 'data-auto-refresh="60"' not in home.text


def test_home_and_workspace_lists_share_clickable_table_style(
    monkeypatch, workspace: Path
):
    """Owner brief: the home Workspaces list and the workspace Tasks lists are
    one table style -- a ``.data-table`` whose rows carry ``data-href`` for
    whole-row navigation, with a hover highlight wired in the shared layout."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "lane-a", status="continue", updated_at=1_700_000_000.0)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        home = client.get("/")
        page = client.get(f"/ws/{ws_id}")

    assert home.status_code == 200
    assert page.status_code == 200
    assert '<table class="data-table">' in home.text
    assert '<table class="data-table">' in page.text
    assert f'data-href="/ws/{ws_id}"' in home.text
    assert f'data-href="/ws/{ws_id}/tasks/lane-a"' in page.text
    # Whole-row navigation and the hover highlight live in the shared layout.
    assert "tr[data-href]" in home.text
    assert "tbody tr:not(.empty-row):hover" in home.text
    assert "closest('tr[data-href]')" in home.text


def test_task_page_links_back_to_workspace(workspace: Path):
    """The task page gets exactly one back-link to its workspace page (goal
    G4; [6c] fixed the nested-block duplicate by moving it into the shared
    chat-page header)."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "my-lane", updated_at=1_700_000_000.0)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}/tasks/my-lane")

    assert resp.status_code == 200
    assert f'<a class="chat-header-back" href="/ws/{ws_id}">← {workspace.name}</a>' in resp.text
    assert resp.text.count(f'href="/ws/{ws_id}"') == 1


def test_task_page_matches_the_polish_brief(workspace: Path):
    """[6c]: the task page is one sticky chat-style header (name over date,
    Takeover right), Status first with its capsule on the heading, a
    header-less Iteration/Generation/Reason table, a required-brief Run block
    with the in progress copy beside a 2/3 Status column, state-colored
    checklist capsules, no Logs section and no inline transcript -- just a
    "Raw transcript" link to the 6d page."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "polish-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("ship the polish\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps(
            {
                "slug": "polish-lane",
                "status": "continue",
                "iteration": 3,
                "generation": 2,
                "reason": "waiting on a decision",
                "updated_at": 1_700_000_000.0,
            }
        ),
        encoding="utf-8",
    )
    (lane / "transcript.md").write_text(
        "FULL INLINE TRANSCRIPT BODY\n", encoding="utf-8"
    )
    logs = lane / "logs"
    logs.mkdir()
    (logs / "run.log").write_text("a log line\n", encoding="utf-8")
    (lane / "checklist.json").write_text(
        json.dumps(
            {
                "steps": {
                    "1-plan": {"title": "Plan", "status": "done"},
                    "2-build": {"title": "Build", "status": "active"},
                    "3-qa": {"title": "QA", "status": "blocked"},
                },
                "remarks": [],
            }
        ),
        encoding="utf-8",
    )

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/polish-lane")

    assert page.status_code == 200, page.text
    html = page.text

    # One back link, in the chat-page header style: name over date, Takeover
    # button on the right.
    assert html.count('class="chat-header-back"') == 1
    assert f'<a class="chat-header-back" href="/ws/{ws_id}">← {workspace.name}</a>' in html
    header = html[html.index("task-header") : html.index("</header>")]
    assert "<strong>polish-lane</strong>" in header
    assert "Updated:" in header
    assert ">Takeover<" in header

    # Status is the first section; the capsule rides the heading and no raw
    # "(continue)"/"(complete)" parenthetical is rendered.
    assert html.index("Status <span") < html.index("<h4>Goal</h4>")
    status = _section(html, "<h4>Status ")
    assert '<span class="badge" data-status="continue">in progress</span>' in status
    assert "(continue)" not in status and "(complete)" not in status

    # Iteration / Generation / Reason: a header-less table.
    meta_start = html.index('<table class="task-meta">')
    meta = html[meta_start : html.index("</table>", meta_start)]
    assert "<thead>" not in meta
    assert "<td>Iteration</td>" in meta and "<td>3</td>" in meta
    assert "<td>Generation</td>" in meta and "<td>2</td>" in meta
    assert "waiting on a decision" in meta

    # Goal box: padded/margined like the transcript box.
    assert '<pre class="goal">ship the polish</pre>' in html

    # Status (about 2/3) and Run (about 1/3) share one grid row.
    assert '<div class="task-columns">' in html
    assert "grid-template-columns: 2fr 1fr" in html

    # Run: required brief, adaptive in progress copy, disabled submit until the
    # brief has text, and no "Launches ..." prose.
    run = _section(html, "<h4>Run</h4>")
    assert "Interrupt this task with an added instruction" in run
    assert 'id="run-submit" disabled' in run
    assert 'name="brief"' in run and "required" in run
    assert "Launches" not in html

    # Checklist capsules are colored by state (done/active/blocked).
    assert '<span class="badge" data-status="done">done</span>' in html
    assert '<span class="badge" data-status="active">active</span>' in html
    assert '<span class="badge" data-status="blocked">blocked</span>' in html

    # No Logs section and no inline transcript; only the "Raw transcript" link
    # (its 6d target page is not built yet).
    assert "<h4>Logs</h4>" not in html
    assert "FULL INLINE TRANSCRIPT BODY" not in html
    assert (
        f'<a href="/ws/{ws_id}/tasks/polish-lane/transcript">Raw transcript</a>'
        in html
    )


def test_task_page_run_copy_restarts_when_not_in_progress(workspace: Path):
    """[6c]: a finished lane reads "Restart this task ..." and shows no literal
    "(complete)" beside the capsule."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "done-lane", status="complete", updated_at=1_700_000_000.0)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/done-lane")

    assert page.status_code == 200
    assert "Restart this task with an added instruction" in page.text
    assert "Interrupt this task" not in page.text
    status = _section(page.text, "<h4>Status ")
    assert "(complete)" not in status and "(continue)" not in status


def test_task_page_auto_refreshes_every_60s(workspace: Path):
    """The task page opts into the shared 1-minute auto-refresh (goal.md:3 /
    plan 3d), reusing the DOMContentLoaded-safe helper (e703a7b)."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "refresh-lane", updated_at=1_700_000_000.0)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/refresh-lane")

    assert page.status_code == 200
    body = page.text
    assert '<main class="container" data-auto-refresh="60">' in body
    assert "window.location.reload()" in body
    # Same ordering guard as the workspace page: the helper lives in <head> and
    # must wait for DOMContentLoaded before reading the attribute.
    refresh_at = body.index("[data-auto-refresh]")
    script_start = body.rindex("<script>", 0, refresh_at)
    script = body[script_start : body.index("</script>", refresh_at)]
    assert script.index("document.addEventListener('DOMContentLoaded'") < script.index(
        "[data-auto-refresh]"
    )


def test_task_status_table_has_margin_and_dividers(workspace: Path):
    """Goal 7 / plan 3d: the header-less Iteration/Generation/Reason table gets
    an outer margin and a divider under each row, like the checklist table."""
    ws_id = _register_workspace(workspace)
    _write_lane(
        workspace,
        "meta-lane",
        status="blocked",
        reason="waiting",
        updated_at=1_700_000_000.0,
    )

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/meta-lane")

    assert page.status_code == 200
    html = page.text
    meta_start = html.index('<table class="task-meta">')
    meta = html[meta_start : html.index("</table>", meta_start)]
    assert "<thead>" not in meta
    assert "<th>" not in meta

    rule = html[html.index(".task-meta {") :]
    rule = rule[: rule.index("}")]
    assert "margin: 0.5rem 0 1rem" in rule

    # Cell padding and row dividers: the status table and the checklist
    # .data-table set neither, so both inherit the same shared table-cell rules.
    bodies = _css_rule_bodies(html)
    assert ".data-table td" in bodies
    for selector in (".task-meta td", ".data-table td"):
        for body in bodies.get(selector, []):
            assert "padding" not in body and "border" not in body, selector


def test_workspace_expander_control_uses_shared_crossed_glyph(workspace: Path):
    """Goal 2 / plan 3d: the add-task control renders as the shared
    ``.expander-plus`` (no literal glyph character) whose strokes cross at the
    centre -- the workspace page carries both the control and the rule."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    html = page.text
    assert 'class="expander-plus"' in html
    assert ">+</summary>" not in html
    start = html.index(".expander-plus::before,")
    rule = html[start : html.index("}", start)]
    assert "top: 50%" in rule
    assert "left: 50%" in rule
    assert "translate(-50%, -50%)" in rule
    assert "scaleY(0)" in html


def test_back_links_are_not_underlined(workspace: Path):
    """Goal 5 / plan 3d: the shared header back link is not underlined,
    consistent across the workspace and task pages; the orphaned .nav-back
    offset rule is gone."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "back-lane", updated_at=1_700_000_000.0)

    with _client() as client:
        css = client.get("/").text
        ws_page = client.get(f"/ws/{ws_id}")
        task_page = client.get(f"/ws/{ws_id}/tasks/back-lane")

    rule = css[css.index(".chat-header-back {") :]
    rule = rule[: rule.index("}")]
    assert "text-decoration: none" in rule
    assert '<a class="chat-header-back" href="/">← Workspaces</a>' in ws_page.text
    assert 'class="chat-header-back"' in task_page.text
    # The old back-link offset rule (workspace.html pre-3c) is gone.
    assert ".nav-back" not in css


def test_add_task_button_disabled_until_valid_name_and_goal(workspace: Path):
    """Goal 9 / plan 3d: the add-task submit starts disabled, and a small inline
    script re-checks on every input -- enabling only when the name sanitizes to
    a usable slug and the goal has non-whitespace text (mirroring
    services.task.sanitize_slug), like the homepage's Add workspace control."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    body = page.text
    button = re.search(
        r"<button\b[^>]*id=\"add-task-submit\"[^>]*>(.*?)</button>", body, re.S
    )
    assert button is not None
    assert button.group(1).strip() == "Add task"
    assert "disabled" in button.group(0)
    # The enabling JS is keyed off both fields and mirrors sanitize_slug.
    assert "getElementById('task-name')" in body
    assert "getElementById('task-goal')" in body
    assert "getElementById('add-task-submit')" in body
    assert "addEventListener('input'" in body
    assert ".normalize('NFKD')" in body
    assert "[^a-z0-9_-]+" in body
    assert "goal.value.trim().length > 0" in body


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "Run"),
        ("continue", "Interrupt"),
        ("blocked", "Restart"),
        ("complete", "Restart"),
        ("mystery", "Restart"),
    ],
)
def test_run_button_text_matches_task_state(workspace: Path, status, expected):
    """Goal 15 / plan 3d: the Run block's label and button share one rule --
    Run for a lane that has never run (no state.json), Interrupt while it is in
    progress, Restart otherwise."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "state-lane", status=status, updated_at=1_700_000_000.0)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/state-lane")

    assert page.status_code == 200
    body = page.text
    assert (
        f'<button type="submit" id="run-submit" disabled>{expected} task</button>'
        in body
    )
    assert f"{expected} this task with an added instruction" in body


def _write_transcript_lane(workspace: Path, slug: str, body: str) -> None:
    """A minimal lane with a ``transcript.md`` for the 6d viewer."""
    lane = workspace / ".backseat" / "tasks" / slug
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "goal.md").write_text(f"goal for {slug}\n", encoding="utf-8")
    (lane / "transcript.md").write_text(body, encoding="utf-8")


def test_task_transcript_page_shares_the_chat_layout(workspace: Path):
    """[6d]: the raw-transcript page reuses the chat layout -- the chat header
    shell with a back link to the task page, and the full transcript below."""
    ws_id = _register_workspace(workspace)
    body = "## USER\nfull transcript body line\n" + "tail chunk " * 20
    _write_transcript_lane(workspace, "talk-lane", body)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}/tasks/talk-lane/transcript")

    assert resp.status_code == 200
    html = resp.text
    # Same header shell as the chat page (shared _chat_page.html).
    assert 'class="chat-header"' in html
    assert html.count('class="chat-header-back"') == 1
    assert (
        f'<a class="chat-header-back" href="/ws/{ws_id}/tasks/talk-lane">← talk-lane</a>'
        in html
    )
    # Full transcript below, untruncated, in the shared transcript box.
    assert '<pre class="transcript">' in html
    assert "full transcript body line" in html
    assert html.count("tail chunk") == 20


def test_task_page_raw_transcript_link_resolves(workspace: Path):
    """[6d] bar (1): the task page's "Raw transcript" link resolves to the new
    page with HTTP 200."""
    ws_id = _register_workspace(workspace)
    _write_transcript_lane(workspace, "talk-lane", "the transcript\n")

    with _client() as client:
        task_page = client.get(f"/ws/{ws_id}/tasks/talk-lane")
        assert task_page.status_code == 200
        href = re.search(r'href="([^"]*talk-lane/transcript)"', task_page.text).group(1)
        assert href == f"/ws/{ws_id}/tasks/talk-lane/transcript"
        followed = client.get(href)

    assert followed.status_code == 200
    assert "the transcript" in followed.text


def test_task_transcript_page_unknown_task_or_workspace_are_404(workspace: Path):
    """[6d] bar (1): an unknown workspace or task slug on the transcript route
    returns 404 rather than crashing."""
    ws_id = _register_workspace(workspace)
    bogus_id = ws_id + 9999

    with _client() as client:
        assert client.get(f"/ws/{bogus_id}/tasks/anything/transcript").status_code == 404
        assert client.get(f"/ws/{ws_id}/tasks/no-such-lane/transcript").status_code == 404


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
    assert '<span class="badge" data-status="weird">weird</span>' in ws_page.text

    assert task_page.status_code == 200
    assert '<span class="badge" data-status="weird">weird</span>' in task_page.text

    # Owner brief: "continue" renders as "in progress" (space) everywhere --
    # never the hyphenated form -- on the home, workspace and task pages.
    for rendered in (page.text, ws_page.text, task_page.text):
        assert "in-progress" not in rendered


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
    reads as ``pending`` ordered by goal.md's mtime."""
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
    """Body of the single non-expandable ``Other chats`` section."""
    return _section(html, "<h4>Other chats</h4>")


def test_workspace_page_sections_tasks_status_and_chat_filtering(
    monkeypatch, workspace: Path
):
    ws_id = _register_workspace(workspace)

    # Status lanes: blocked -> "needs attention", continue -> "in progress",
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
    # 22 continue lanes (no state.json) so Active is exercised uncapped; their
    # goal mtimes keep them below the three status lanes.
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

    active = _section(resp.text, "<h4>Active tasks</h4>")
    completed = _section(resp.text, "<h4>Completed tasks</h4>")
    chats = _section(resp.text, "<h4>Other chats</h4>")

    # The brief's order: Active -> (add-task expander) -> Completed -> one
    # Other chats table. The duplicated "Recent tasks" (== "Completed tasks")
    # and the old two-list chat split are gone.
    heading_positions = [
        resp.text.index(h)
        for h in ("<h4>Active tasks</h4>", "<h4>Completed tasks</h4>",
                  "<h4>Other chats</h4>")
    ]
    assert heading_positions == sorted(heading_positions)
    add_task = resp.text.index("id=\"add-task\"")
    assert add_task > resp.text.index("</table>", heading_positions[0])
    assert add_task < heading_positions[1]
    assert "Recent tasks" not in resp.text
    assert "Recent chats" not in resp.text
    assert '<details id="other-chats">' not in resp.text

    # Active: badge text per status, complete lane excluded, uncapped
    # (still lists every incomplete lane, now 24).
    inc_rows = _data_rows(active)
    assert len(inc_rows) == 24  # 25 lanes minus the complete one
    blocked_row = next(r for r in inc_rows if ">blocked-lane<" in r)
    assert '<span class="badge" data-status="blocked">needs attention</span>' in blocked_row
    continue_row = next(r for r in inc_rows if ">continue-lane<" in r)
    assert '<span class="badge" data-status="continue">in progress</span>' in continue_row
    assert all("done-lane" not in r for r in inc_rows)
    assert ">filler-00<" in active  # oldest active lane still listed

    # Completed tasks: only the complete lane.
    comp_rows = _data_rows(completed)
    assert len(comp_rows) == 1
    assert ">done-lane<" in comp_rows[0]
    assert '<span class="badge" data-status="complete">complete</span>' in comp_rows[0]

    # Other chats: one plain section, workspace-scoped, <meta> marker sessions
    # excluded, newest first, prompt in a fixed-width cell, no session column.
    chat_rows = _data_rows(chats)
    assert len(chat_rows) == 2
    assert "chatting about widgets" in chat_rows[0]
    assert 'href="/chat/opencode/ses_ws_new?ws=1"' in chat_rows[0]
    assert 'class="prompt"' in chat_rows[0]
    assert "chatting about gadgets" in chat_rows[1]
    assert 'href="/chat/claude_code/ses_ws_old?ws=1"' in chat_rows[1]
    assert "ses_ws_marker" not in resp.text
    assert "managed" not in resp.text
    assert "ses_elsewhere" not in resp.text
    assert "other project chatter" not in resp.text
    assert "<th>Session</th>" not in resp.text


def test_workspace_page_other_chats_is_one_capped_list(
    monkeypatch, workspace: Path
):
    """12 clean workspace chats + a marker + an empty transcript + an empty
    prompt: the single "Other chats" table shows the ten newest non-task chats
    and nothing else -- no "Recent chats" split, no expandable overflow, and
    empty sessions dropped (owner brief)."""
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
    # A session whose transcript is whitespace-only and one whose only prompt
    # line is the bare "USER" label / blank: both are ignored.
    refs.append(_Ref("opencode:ses_split_empty", mtime=998.0, project=str(workspace)))
    transcripts["opencode:ses_split_empty"] = "   \n  "
    refs.append(_Ref("opencode:ses_split_noprompt", mtime=997.0, project=str(workspace)))
    transcripts["opencode:ses_split_noprompt"] = "## USER\n"
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    assert "Recent chats" not in resp.text
    chats = _other_chats_section(resp.text)
    rows = _data_rows(chats)

    # Ten rows, newest ten first (ses_split_11 down to ses_split_02).
    assert len(rows) == 10
    assert 'href="/chat/opencode/ses_split_11?ws=1"' in rows[0]
    assert 'href="/chat/opencode/ses_split_02?ws=1"' in rows[-1]
    # ...and the two oldest fell off the 10-cap.
    assert "ses_split_01" not in chats
    assert "ses_split_00" not in chats
    # Managed and empty sessions appear nowhere.
    assert "ses_split_marker" not in resp.text
    assert "ses_split_empty" not in resp.text
    assert "ses_split_noprompt" not in resp.text
    # It is one plain table, not an expandable <details>.
    assert '<details id="other-chats">' not in resp.text
    assert "<th>Prompt</th>" in chats
    assert 'class="prompt"' in chats
    assert "<th>Session</th>" not in resp.text


def test_other_chats_strips_quote_prefix_in_rendered_prompt(
    monkeypatch, workspace: Path
):
    """Goal 6 / plan 3e: a multi-line quoted prompt renders without any ``>``
    marker -- the collapsed text lands in the prompt cell, and no escaped
    ``&gt;`` survives in the table."""
    ws_id = _register_workspace(workspace)
    refs = [_Ref("opencode:ses_quoted", mtime=300.0, project=str(workspace))]
    transcripts = {
        "opencode:ses_quoted": (
            "## USER\n> plan the widget\n> then ship it\n"
            "## ASSISTANT\n> done"
        )
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    chats = _other_chats_section(page.text)
    row = _data_rows(chats)[0]
    assert "plan the widget then ship it" in row
    assert "&gt;" not in row  # the quoting marker is gone entirely


def test_other_chats_table_has_provider_column(monkeypatch, workspace: Path):
    """Goal 16 / plan 3e: a Provider column sits between Prompt and Updated,
    read from the row's own provider (the key prefix), exact per row."""
    ws_id = _register_workspace(workspace)
    refs = [
        _Ref("opencode:ses_col_a", mtime=300.0, project=str(workspace)),
        _Ref("kiro_ide_v2:sess_col_b", mtime=200.0, project=str(workspace)),
    ]
    _stub_chat_history(
        monkeypatch,
        refs,
        {"opencode:ses_col_a": "chat a", "kiro_ide_v2:sess_col_b": "chat b"},
    )

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    chats = _other_chats_section(page.text)
    # Order: Prompt, Provider, Updated.
    assert (
        chats.index("<th>Prompt</th>")
        < chats.index("<th>Provider</th>")
        < chats.index("<th>Updated</th>")
    )
    rows = _data_rows(chats)  # newest first
    assert "<td>opencode</td>" in rows[0]
    assert "<td>kiro_ide_v2</td>" in rows[1]
    assert 'href="/chat/opencode/ses_col_a?ws=' in rows[0]


def test_other_chats_empty_state_spans_three_columns(monkeypatch, workspace: Path):
    """Goal 16 / plan 3e: with no chats the empty-state row spans the new
    three-column table."""
    ws_id = _register_workspace(workspace)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    chats = _other_chats_section(page.text)
    assert '<td colspan="3">No other chats.</td>' in chats


def test_other_chats_prompt_font_matches_date(monkeypatch, workspace: Path):
    """Goal 6 / plan 3e: the Other-chats prompt takes the date cell's size
    (one shared ``0.8em`` rule for ``.data-table small`` and ``.prompt``); the
    prompt keeps only its fixed-width family."""
    ws_id = _register_workspace(workspace)
    refs = [_Ref("opencode:ses_size", mtime=300.0, project=str(workspace))]
    _stub_chat_history(monkeypatch, refs, {"opencode:ses_size": "chat body"})

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    html = page.text
    shared = html.index(".data-table small,")
    rule_end = html.index("}", shared)
    shared_rule = html[shared:rule_end]
    assert ".prompt" in shared_rule
    assert "font-size: 0.8em" in shared_rule

    # The prompt's own rule keeps the family and no longer sets a size.
    prompt_start = html.index(".prompt {", rule_end)
    prompt_rule = html[prompt_start : html.index("}", prompt_start)]
    assert "font-family: ui-monospace" in prompt_rule
    assert "font-size" not in prompt_rule


def test_other_chats_recovers_workspace_from_provider_store(
    monkeypatch, workspace: Path
):
    """A chat whose ``project`` is unusable still lands in "Other chats" when
    its provider store names this workspace -- the recovery the suggestion
    list already uses -- so membership is not decided by the basename alone,
    and a newer chat from another project cannot take the row."""
    ws_id = _register_workspace(workspace)
    session_dir = workspace / "_store"
    session_dir.mkdir()
    (session_dir / "session.json").write_text(
        json.dumps({"workspacePaths": [str(workspace)]}), encoding="utf-8"
    )
    refs = [
        _Ref(
            "kiro_ide_v2:sess_recovered",
            mtime=300.0,
            project=None,
            locator=str(session_dir),
        ),
        _Ref("opencode:ses_elsewhere_newer", mtime=900.0, project="elsewhere"),
    ]
    transcripts = {
        "kiro_ide_v2:sess_recovered": "recovered from the store",
        "opencode:ses_elsewhere_newer": "other project chatter",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    assert "recovered from the store" in _other_chats_section(resp.text)
    assert "other project chatter" not in resp.text


def test_other_chats_bounds_the_transcripts_it_reads(
    monkeypatch, workspace: Path
):
    """Transcript reads are capped at ``limit * 20`` rows of this workspace
    (floor 50 -- 200 for the ten-row table), never every candidate.

    What is *not* bounded is the candidate window: the workspace filter has
    already run over each provider's own window by the time this cap applies,
    so a chatty provider still cannot starve the others. Only the parse-per-row
    half -- the part that made ``/ws/<id>`` slow -- is bounded here."""
    ws_id = _register_workspace(workspace)
    refs = []
    transcripts = {}
    for i in range(210):
        key = f"opencode:ses_bound_{i:03d}"
        refs.append(_Ref(key, mtime=900.0 - i, project=str(workspace)))
        transcripts[key] = f"managed {_MARKER}"
    _stub_chat_history(monkeypatch, refs, transcripts)

    from backseat_harness import history

    reads: list[int] = []
    batched = history.load_sessions

    def _spy(keys, providers=None) -> dict[str, str]:
        requested = list(keys)
        reads.append(len(requested))
        return batched(requested, providers=providers)

    monkeypatch.setattr(history, "load_sessions", _spy)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    assert sum(reads) == 200, f"read {sum(reads)} transcripts, the cap is 200"
    # Every candidate is a managed session, so nothing survives the filter.
    assert _data_rows(_other_chats_section(page.text)) == []


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
    completed = _section(resp.text, "<h4>Completed tasks</h4>")
    assert "No completed tasks." in completed
    assert _data_rows(completed) == []


def test_prompt_excerpt_normalizes_and_truncates():
    """The prompt string: leading <meta> markers dropped, first ``## USER``
    turn body preferred, leading blank lines and a bare ``USER`` role label
    skipped, newlines/tabs collapsed to spaces, cut at 120 chars with "..."
    appended when truncated, whole-text fallback without headers (owner
    brief)."""
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
    assert excerpt("## USER\n" + "x" * 200) == "x" * 120 + "..."
    assert excerpt("plain text\n\twith lines") == "plain text with lines"
    assert excerpt("") == ""
    # A bare "USER" leading line and leading empty lines are skipped.
    assert excerpt("USER\n\nhello there") == "hello there"
    assert excerpt("\n\n  \nUSER\ncontent") == "content"


def test_prompt_excerpt_strips_blockquote_prefix():
    """Goal 6 / plan 3e: chat_fork quotes USER/ASSISTANT lines with a leading
    ``> ``; the excerpt drops that marker from *every* line before collapsing,
    so the listing shows the prompt text rather than the quoting."""
    excerpt = chat_search_service._prompt_excerpt

    quoted = (
        "## USER\n> first line\n> second line\n> third line\n"
        "## ASSISTANT\n> a reply"
    )
    assert excerpt(quoted) == "first line second line third line"
    # A bare ">" (no space) and quoted blank lines carry no prompt text.
    assert excerpt("## USER\n>\n>\n> hello") == "hello"
    # A headerless transcript is stripped line by line too.
    assert excerpt("> one\n> two") == "one two"


def test_workspace_page_reads_transcripts_in_one_batch(monkeypatch, workspace: Path):
    """Regression guard for the slow ``/ws/<id>``: the cheap listing, one batch.

    The pre-fix path asked chat-mother-forker for each row's transcript one at
    a time, and every one of those tiered searches re-parses *every* stored
    conversation of *every* provider -- measured at 2.4s per row against this
    machine's 344 stored conversations, 147s end-to-end for one workspace
    page. The page must therefore list its candidates from the unparsed
    ``list_session_refs`` scan -- per-provider windows, never ``find_chats``,
    whose single global recency cut a chatty provider can fill with rows the
    workspace filter then throws away -- and it must ask for the transcripts
    once, with all candidate keys at once.
    """
    ws_id = _register_workspace(workspace)
    # 11 chats so the single "Other chats" table's 10-cap drops the oldest; the
    # one batch must still cover every candidate at once.
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
    window: list[list[object]] = []
    searches: list[str] = []
    batched = history.load_sessions
    listed = history.list_session_refs

    # Record any ``find_chats`` call so the page can be held to the cheap scan.
    real_load = history._load

    def _spy_load(attr: str):
        impl = real_load(attr)
        if attr != "find_chats":
            return impl

        def _find_chats(query="", providers=None, limit=None):
            searches.append(query)
            return impl(query, providers=providers, limit=limit)

        return _find_chats

    def _spy_list(limit=None):
        window.append(limit)
        return listed(limit=limit)

    def _spy(keys, providers=None) -> dict[str, str]:
        requested = list(keys)
        calls.append(requested)
        return batched(requested, providers=providers)

    monkeypatch.setattr(history, "_load", _spy_load)
    monkeypatch.setattr(history, "list_session_refs", _spy_list)
    monkeypatch.setattr(history, "load_sessions", _spy)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    # The window is the whole unparsed scan -- no global recency cut (None) --
    # and the parse-per-row search is never reached.
    assert window == [None], f"candidate window was cut to {window!r}"
    assert searches == [], "the workspace page must not call find_chats"
    assert len(calls) == 1, f"transcripts were fetched {len(calls)} times, not once"
    expected_keys = {r.key for r in refs}
    assert set(calls[0]) == expected_keys
    # The one batch covers every rendered row; the single Other chats table
    # keeps the ten newest and drops the oldest off the cap.
    other = _other_chats_section(page.text)
    assert "chat about widgets" in other
    assert "chat about gadgets" in other
    assert "filler chat 07" in other  # the tenth row, right on the cap
    assert "filler chat 08" not in other  # the eleventh falls off


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


def test_chat_page_fork_button_back_link_and_transcript_styling(monkeypatch):
    """Owner brief: the chat header's back link is blue like other pages, the
    Fork button is compact so it does not tower over the back link, and the
    transcript box has padding and margin."""
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )
    with _client() as client:
        resp = client.get("/chat/cline/sess_fork")

    assert resp.status_code == 200
    # Back link is blue: the rule uses the accent color, not muted.
    back_start = resp.text.index(".chat-header-back {")
    back_rule = resp.text[back_start : resp.text.index("}", back_start)]
    assert "color: var(--accent)" in back_rule
    assert "var(--muted)" not in back_rule
    # Compact header button (was Pico's tall default).
    btn_start = resp.text.index(".chat-header .chat-continue-form button {")
    btn_rule = resp.text[btn_start : resp.text.index("}", btn_start)]
    assert "padding: 0.3rem 0.85rem" in btn_rule
    assert "font-size: 0.85rem" in btn_rule
    # Transcript box has padding and margin.
    assert '<pre class="transcript">' in resp.text
    tx_start = resp.text.index(".transcript {")
    tx_rule = resp.text[tx_start : resp.text.index("}", tx_start)]
    assert "padding: 0.85rem 1rem" in tx_rule
    assert "margin: 0.5rem 0 1rem" in tx_rule


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


def test_build_fork_command_passes_initial_prompt_to_each_provider():
    """The fork launch command carries the initial prompt as an argument for
    every fork provider (owner brief: kilo/kiro forked without it)."""
    from backseat_harness.web.routes import search as search_routes

    prompt = "Please fork kilo chat session ses_x"
    assert search_routes._build_fork_command("kiro", prompt) == [
        "kiro-cli",
        "chat",
        prompt,
    ]
    assert search_routes._build_fork_command("opencode", prompt) == [
        "opencode",
        "--prompt",
        prompt,
    ]
    assert search_routes._build_fork_command("claude", prompt) == ["claude", prompt]
    with pytest.raises(ValueError, match="Unknown fork provider"):
        search_routes._build_fork_command("bogus", prompt)


def test_fork_prompt_names_the_source_provider_and_session():
    """The prompt names the source provider, e.g.
    "Please fork kilo chat session XXXXX" (owner brief)."""
    from backseat_harness.web.routes import search as search_routes

    assert (
        search_routes._fork_prompt("kilo", "sess_42")
        == "Please fork kilo chat session sess_42"
    )
    assert (
        search_routes._fork_prompt("kiro", "sess_43")
        == "Please fork kiro chat session sess_43"
    )


def _capture_fork_command(monkeypatch) -> dict:
    """Capture the argv the fork route hands to the PTY manager -- the real
    launch command -- without spawning a child."""
    from backseat_harness.web.shell.manager import PTYSessionManager

    captured: dict[str, object] = {}

    def _fake_create_session(self, cmd, cwd, cols=None, rows=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return "abc123def456"

    monkeypatch.setattr(PTYSessionManager, "create_session", _fake_create_session)
    return captured


def test_search_fork_launches_kiro_with_initial_prompt(monkeypatch):
    """A kiro_ide source chat forks via kiro-cli, and the launch command ends
    with the initial prompt (no delayed PTY write)."""
    refs = [_Ref("kiro_ide:sess_cur", mtime=200.0, project=None)]
    _stub_chat_history(monkeypatch, refs, {"kiro_ide:sess_cur": "kiro ide chat"})
    captured = _capture_fork_command(monkeypatch)

    with _client() as client:
        resp = client.post(
            "/search/fork",
            data={
                "conversation_id": "sess_cur",
                "current_session_key": "kiro_ide:sess_cur",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert captured["cmd"] == [
        "kiro-cli",
        "chat",
        "Please fork kiro_ide chat session sess_cur",
    ]


def test_search_fork_launches_opencode_with_initial_prompt_for_kilo(monkeypatch):
    """A kilo source chat forks via the opencode-family CLI (rule 2 picks the
    supported opencode chat), and the prompt names the kilo source."""
    refs = [
        _Ref("opencode:ses_recent", mtime=100.0, project=None),
        _Ref("kilo:sess_k", mtime=200.0, project=None),
    ]
    _stub_chat_history(
        monkeypatch, refs, {"opencode:ses_recent": "x", "kilo:sess_k": "y"}
    )
    captured = _capture_fork_command(monkeypatch)

    with _client() as client:
        resp = client.post(
            "/search/fork",
            data={
                "conversation_id": "sess_k",
                "current_session_key": "kilo:sess_k",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert captured["cmd"] == [
        "opencode",
        "--prompt",
        "Please fork kilo chat session sess_k",
    ]


# --- step 3a: shared polish ----------------------------------------------------


def test_data_table_rows_hover_and_links_underline_only_on_hover():
    """Goal 4: populated rows highlight; in-table links stay quiet (no
    underline) until their row is hovered, then underline and accent-light."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    css = page.text
    assert ".data-table tbody tr:not(.empty-row):hover" in css

    a_start = css.index(".data-table a {")
    a_rule = css[a_start : css.index("}", a_start)]
    assert "text-decoration: none" in a_rule

    h_sel = ".data-table tbody tr:not(.empty-row):not(:has(a.task-link:hover)):hover"
    h_start = css.index(f"{h_sel} a:not(.task-link) {{")
    h_rule = css[h_start : css.index("}", h_start)]
    assert "text-decoration: underline" in h_rule
    assert "color: var(--accent-hover)" in h_rule


def test_task_links_underline_only_under_their_own_pointer():
    """Homepage task links (``.task-link``) are excluded from the row-hover
    rule, so they underline only while hovered themselves; and the row's other
    links stay quiet while a task link is the thing hovered (goal 4 / owner
    feedback on the Tasks cell)."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    css = page.text
    # Excluded from the row-hover underline.
    row_rule = css[css.index(".data-table tbody tr:not(.empty-row):not(:has(") :]
    row_rule = row_rule[: row_rule.index("{")]
    assert ":not(.task-link)" in row_rule
    assert ":has(a.task-link:hover)" in row_rule
    # Own-hover rule underlines the task link itself.
    t_start = css.index(".data-table a.task-link:hover {")
    t_rule = css[t_start : css.index("}", t_start)]
    assert "text-decoration: underline" in t_rule
    assert "color: var(--accent-hover)" in t_rule


def test_workspace_empty_and_chat_rows_use_shared_hover(monkeypatch, workspace: Path):
    """Empty listings are inert .empty-row rows; Other chats rows are populated
    and navigable, so they get the shared hover (goal 4)."""
    ws_id = _register_workspace(workspace)
    refs = [_Ref("opencode:ses_hover", mtime=300.0, project=str(workspace))]
    _stub_chat_history(monkeypatch, refs, {"opencode:ses_hover": "chat body"})

    with _client() as client:
        page = client.get(f"/ws/{ws_id}")

    assert page.status_code == 200
    html = page.text
    assert '<tr class="empty-row">' in html
    chats = _other_chats_section(html)
    assert 'data-href="/chat/opencode/ses_hover?ws=' in chats
    assert ".data-table tbody tr:not(.empty-row):hover" in html


def test_task_checklist_rows_hover_but_status_table_does_not(workspace: Path):
    """The checklist is the shared .data-table, so its data-href-less rows
    hover; the key/value .task-meta status table is not, so it never does
    (goal 4)."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "hover-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal\n", encoding="utf-8")
    (lane / "checklist.json").write_text(
        json.dumps({"steps": {"1-a": {"title": "A", "status": "done"}}, "remarks": []}),
        encoding="utf-8",
    )

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/hover-lane")

    assert page.status_code == 200
    html = page.text
    assert '<table class="task-meta">' in html
    meta_start = html.index('<table class="task-meta">')
    meta = html[meta_start : html.index("</table>", meta_start)]
    assert 'class="data-table"' not in meta
    cl_start = html.index('<table class="data-table">')
    checklist = html[cl_start : html.index("</table>", cl_start)]
    assert "<td>1-a</td>" in checklist
    assert ".data-table tbody tr:not(.empty-row):hover" in html


def test_expander_glyph_strokes_share_centre_and_collapse_to_minus():
    """Goal 2: Pico's summary::after margin/float are cleared so both strokes
    cross at the centre (was "-|"); expanded collapses to a clean minus."""
    with _client() as client:
        css = client.get("/").text

    start = css.index(".expander-plus::before,")
    rule = css[start : css.index("}", start)]
    assert "position: absolute" in rule
    assert "top: 50%" in rule
    assert "left: 50%" in rule
    assert "translate(-50%, -50%)" in rule
    assert "margin: 0" in rule
    assert "float: none" in rule
    assert "scaleY(0)" in css


def test_transcript_box_is_height_bounded_and_scrolls_vertically():
    """Goal 14: the transcript box has a bounded height so it scrolls
    vertically itself, and overscroll stays auto so it chains to the page."""
    with _client() as client:
        css = client.get("/").text

    start = css.index(".transcript {")
    rule = css[start : css.index("}", start)]
    assert "max-height" in rule
    assert "overflow: auto" in rule
    assert "overscroll-behavior: auto" in rule


def test_textareas_use_compact_font():
    """Goal 8: one shared compact textarea size, so the run brief matches the
    add forms rather than inheriting the 14px body size."""
    with _client() as client:
        html = client.get("/").text

    assert "    textarea {\n      font-size: 0.85rem;\n    }" in html


def test_top_header_text_larger_and_prompt_cannot_overflow():
    """Goal 12: the top header line is modestly larger, and a long chat prompt
    excerpt is clipped with an ellipsis so it fits the sticky bar."""
    with _client() as client:
        css = client.get("/").text

    start = css.index(".chat-header-prompt > strong {")
    rule = css[start : css.index("}", start)]
    assert "font-size: 1.15rem" in rule
    assert "overflow: hidden" in rule
    assert "text-overflow: ellipsis" in rule
    assert "white-space: nowrap" in rule


def test_header_action_button_sits_below_back_link():
    """Goal 13: the Continue/Fork/Takeover form is bottom-aligned and offset so
    it cannot overlap the back link."""
    with _client() as client:
        css = client.get("/").text

    start = css.index(".chat-header .chat-continue-form {")
    rule = css[start : css.index("}", start)]
    assert "align-self: flex-end" in rule
    assert "margin: 0.6rem 0 0" in rule


# --- step 4-test: edge coverage named in the QA plan ---------------------------


def test_auto_refresh_keeps_homepage_poll_and_pauses_on_edit(workspace: Path):
    """Goal 1 / plan edge cases: the homepage keeps its own HTMX ``every 60s``
    poll (it must not also carry the reload hook), and the shared reload helper
    defers while the user is typing or an expander is open (rendered source)."""
    ws_id = _register_workspace(workspace)

    with _client() as client:
        home = client.get("/")
        page = client.get(f"/ws/{ws_id}")

    assert home.status_code == 200 and page.status_code == 200

    # The homepage poll is intact -- the other half of the page split.
    assert 'hx-trigger="every 60s, workspaceAdded from:body"' in home.text
    assert 'data-auto-refresh="60"' not in home.text

    # The reload helper defers while editing or with an open + expander.
    body = page.text
    assert "details.expander[open]" in body
    assert "/^(input|textarea|select)$/i" in body
    assert "window.location.reload()" in body


# --- shell header: back link, command heading, disconnect ----------------------


def test_shell_page_header_matches_chat_style_with_back_link(tmp_path: Path):
    """The shell page reuses the shared chat header: a back link, the command
    as the top line over the session id, and a red Disconnect control."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(
                f"/shell/{session_id}",
                params={"from": "/ws/1/tasks/lane-a", "label": "my-ws"},
            )
            assert page.status_code == 200
            header = page.text[
                page.text.index('<header class="chat-header">') : page.text.index(
                    "</header>"
                )
            ]
            assert (
                '<a class="chat-header-back" href="/ws/1/tasks/lane-a">← my-ws</a>'
                in header
            )
            assert f"<strong>{' '.join(cmd)}</strong>" in header
            assert f"<small>{session_id}</small>" in header
            assert f'action="/shell/{session_id}/stop"' in header
            assert ">Disconnect<" in header
            # The disconnect button is red, not the shared accent fill.
            assert ".shell-disconnect" in page.text
            assert "#f85149" in page.text
        finally:
            _kill_session(session_id)


def test_shell_page_back_link_falls_back_to_same_origin_referer(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(
                f"/shell/{session_id}",
                headers={"referer": f"http://localhost:{_PORT}/chat/opencode/ses_x"},
            )
            assert (
                '<a class="chat-header-back" href="/chat/opencode/ses_x">← Chat</a>'
                in page.text
            )
        finally:
            _kill_session(session_id)


def test_shell_page_back_link_rejects_foreign_referer(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(
                f"/shell/{session_id}",
                headers={"referer": "http://evil.com/chat/opencode/ses_x"},
            )
            assert (
                '<a class="chat-header-back" href="/">← Workspaces</a>' in page.text
            )
        finally:
            _kill_session(session_id)


def test_shell_page_back_link_rejects_protocol_relative_from(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(
                f"/shell/{session_id}",
                params={"from": "//evil.com/x", "label": "x"},
            )
            assert (
                '<a class="chat-header-back" href="/">← Workspaces</a>' in page.text
            )
        finally:
            _kill_session(session_id)


def test_shell_stop_kills_session_and_redirects_back(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        resp = client.post(
            f"/shell/{session_id}/stop",
            data={"back_url": "/ws/1/tasks/lane-a"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/ws/1/tasks/lane-a"
    assert _shell_manager().get_session(session_id) is None


def test_shell_stop_unsafe_back_url_falls_back_to_home(tmp_path: Path):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        resp = client.post(
            f"/shell/{session_id}/stop",
            data={"back_url": "https://evil.com/"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_shell_stop_unknown_session_still_redirects():
    with _client() as client:
        resp = client.post("/shell/000000000000/stop", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_task_takeover_form_carries_back_nav_hidden_inputs(workspace: Path):
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "back-nav-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps(
            {
                "slug": "back-nav-lane",
                "status": "blocked",
                "iteration": 0,
                "generation": 1,
                "reason": "Pick a palette color",
                "updated_at": 1_700_000_300.0,
            }
        ),
        encoding="utf-8",
    )

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/back-nav-lane")

    assert _hidden_input(page.text, "back_url") == f"/ws/{ws_id}/tasks/back-nav-lane"
    assert _hidden_input(page.text, "back_label") == workspace.name


def test_chat_continue_form_carries_back_nav_hidden_inputs(monkeypatch):
    from backseat_harness import history

    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: "chat body" for k in keys}
    )

    with _client() as client:
        resp = client.get("/chat/opencode/ses_backnav")

    assert _hidden_input(resp.text, "back_url") == "/chat/opencode/ses_backnav"
    assert _hidden_input(resp.text, "back_label") == "Workspaces"


def test_search_fork_redirect_carries_back_nav_params(monkeypatch):
    refs = [_Ref("kiro_ide:sess_cur", mtime=200.0, project=None)]
    _stub_chat_history(monkeypatch, refs, {"kiro_ide:sess_cur": "kiro ide chat"})
    _capture_fork_command(monkeypatch)

    with _client() as client:
        resp = client.post(
            "/search/fork",
            data={
                "conversation_id": "sess_cur",
                "current_session_key": "kiro_ide:sess_cur",
                "back_url": "/chat/kiro_ide/sess_cur",
                "back_label": "Workspaces",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/shell/abc123def456?from=%2Fchat%2Fkiro_ide%2Fsess_cur&label=Workspaces"
    )


def test_compact_form_controls_override_pico_specificity():
    """Owner brief: single-line inputs must actually be compact. Pico pins
    their height from its own 1.5 ``--pico-line-height`` variable, and its
    ``[type=submit]`` button group out-specifies a bare ``button`` selector --
    so a lower-specificity padding override silently does nothing. The compact
    scale therefore has to ride selectors that match Pico's specificity."""
    with _client() as client:
        page = client.get("/")

    body = page.text
    # Compact spacing values replace Pico's 0.75rem vertical default for
    # every form control (the add forms and the search row included).
    assert "--pico-form-element-spacing-vertical: 0.25rem" in body
    assert "--pico-form-element-spacing-horizontal: 0.6rem" in body

    rules = _css_rule_bodies(body)
    # Pico's explicit input height is released, and the compact line-height
    # rides the same high-specificity selector (verified in the browser:
    # 40px -> 32px).
    input_rule = rules.get(
        "input:not([type=checkbox],[type=radio],[type=range])", [""]
    )[0]
    assert "height: auto" in input_rule
    assert "line-height: 1.25" in input_rule
    # The button group matches Pico's [type=submit] specificity.
    button_rule = rules.get(
        "textarea, [type=submit], [type=reset], [type=button], button, [role=button]",
        [""],
    )[0]
    assert "line-height: 1.25" in button_rule
    # The search input uses the compact font size.
    search_rule = rules.get(".search-form input", [""])[0]
    assert "font-size: 0.85rem" in search_rule


def test_homepage_header_and_search_form():
    """The homepage carries the shared chat header (no back link) and the
    search form: an input covering the left half with a small submit button."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    header = page.text[
        page.text.index('<header class="chat-header">') : page.text.index("</header>")
    ]
    assert "<strong>backseat-harness</strong>" in header
    assert "<small>Buckle up</small>" in header
    assert '<a class="chat-header-back"' not in header

    assert 'id="search-form"' in page.text
    assert 'name="q"' in page.text
    assert 'id="search-submit"' in page.text
    assert 'hx-get="/search/results"' in page.text
    # The submit button stays disabled while the search is in flight.
    assert "htmx:beforeRequest" in page.text
    assert "htmx:afterRequest" in page.text
    # The input takes the left half; the button is not stretched.
    assert ".search-form input" in page.text
    assert "width: 50%" in page.text


def test_search_results_fragment_interleaves_task_and_other_rows(
    monkeypatch, workspace: Path
):
    """The fragment interleaves task chats (deduped, enriched) with other
    chats in chat-search order: a task row links to the task page with the
    goal prompt, task name and status capsule; an other-chat row links to
    the chat page with a plain N/A status.

    The lane's identity comes from ``binding.json`` (its on-disk transcript
    markers are redacted to ``<task-root>``, so they can never be matched
    against a chat's real root UUID)."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "search-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal for search lane\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps(
            {
                "slug": "search-lane",
                "status": "blocked",
                "iteration": 0,
                "generation": 1,
                "reason": "waiting",
                "updated_at": 1_700_000_300.0,
            }
        ),
        encoding="utf-8",
    )
    (lane / "binding.json").write_text(
        json.dumps(
            {
                "v": 1,
                "root_uuid": _MARKER_ROOT_UUID,
                "provider": "opencode",
                "generations": [{"n": 1, "native_id": "ses_task"}],
            }
        ),
        encoding="utf-8",
    )

    # Two provider chats carry the same task marker: they collapse into one
    # row, keeping the first hit's position.
    refs = [
        _Ref("opencode:ses_task", mtime=400.0, project=str(workspace)),
        _Ref("claude_code:ses_task_dup", mtime=350.0, project=str(workspace)),
        _Ref("opencode:ses_plain", mtime=200.0),
    ]
    transcripts = {
        "opencode:ses_task": f"{_MARKER}\n## USER\nfix the thing\n",
        "claude_code:ses_task_dup": f"{_MARKER}\n## USER\nfix the thing\n",
        "opencode:ses_plain": "a plain chat about refactors",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    html = resp.text

    # The deduped task row: goal prompt, task name, status capsule, task link.
    assert f'data-href="/ws/{ws_id}/tasks/search-lane"' in html
    assert "goal for search lane" in html
    assert "search-lane" in html
    assert '<span class="badge" data-status="blocked">needs attention</span>' in html
    # The duplicate hit was skipped: exactly one row links to the task page.
    assert html.count(f'data-href="/ws/{ws_id}/tasks/search-lane"') == 1

    # The other-chat row: parsed prompt, provider, plain N/A, chat link.
    assert 'data-href="/chat/opencode/ses_plain"' in html
    assert "a plain chat about refactors" in html
    assert "N/A" in html

    # Chat-search order is preserved: the task row precedes the other row.
    assert html.index("goal for search lane") < html.index("a plain chat about refactors")

    # Both rows carry the formatted mtime.
    assert datetime.fromtimestamp(400.0).strftime("%Y-%m-%d %H:%M") in html
    assert datetime.fromtimestamp(200.0).strftime("%Y-%m-%d %H:%M") in html


def test_search_results_caps_at_twenty_rows(monkeypatch):
    refs = [_Ref(f"opencode:ses_{i:02d}", mtime=100.0 + i) for i in range(25)]
    _stub_chat_history(monkeypatch, refs, {})

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    assert resp.text.count('data-href="/chat/') == 20


def test_search_results_empty(monkeypatch):
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        resp = client.get("/search/results", params={"q": "nothing"})

    assert resp.status_code == 200
    assert "No sessions found." in resp.text
    assert "<table" not in resp.text


def test_search_results_collapses_operator_chat_into_task_row(
    monkeypatch, workspace: Path
):
    """An operator session for a task collapses into that task's row: the
    row keeps the first hit's position and metadata (the operator chat's
    mtime), shows the goal prompt and status capsule, and the operator and
    managed chat bodies never appear as rows of their own."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "op-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal for op lane\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps(
            {
                "slug": "op-lane",
                "status": "blocked",
                "iteration": 0,
                "generation": 1,
                "reason": "waiting",
                "updated_at": 1_700_000_300.0,
            }
        ),
        encoding="utf-8",
    )
    (lane / "binding.json").write_text(
        json.dumps(
            {
                "v": 1,
                "root_uuid": _MARKER_ROOT_UUID,
                "generations": [{"n": 1, "native_id": "ses_managed"}],
            }
        ),
        encoding="utf-8",
    )

    # The operator chat is the newer hit, so it comes first and supplies the
    # row's metadata; the managed chat for the same lane is then skipped.
    refs = [
        _Ref("opencode:ses_op", mtime=500.0, project=str(workspace)),
        _Ref("opencode:ses_managed", mtime=400.0, project=str(workspace)),
    ]
    transcripts = {
        "opencode:ses_op": (
            "<meta>OPERATOR SESSION op-lane review</meta>\n"
            "## USER\noperator chat body\n"
        ),
        "opencode:ses_managed": f"{_MARKER}\n## USER\nmanaged chat body\n",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    html = resp.text
    # Exactly one row, for the task: both hits collapsed into it.
    assert html.count(f'data-href="/ws/{ws_id}/tasks/op-lane"') == 1
    assert "goal for op lane" in html
    assert '<span class="badge" data-status="blocked">needs attention</span>' in html
    # Neither chat body shows up as its own row.
    assert "operator chat body" not in html
    assert "managed chat body" not in html
    # The first hit (operator, newer) supplies the Updated timestamp.
    assert datetime.fromtimestamp(500.0).strftime("%Y-%m-%d %H:%M") in html
    assert datetime.fromtimestamp(400.0).strftime("%Y-%m-%d %H:%M") not in html


def test_search_results_drops_operator_chat_without_a_task(monkeypatch):
    """A task-less operator session (phase-only tag) maps to no lane, so it
    is dropped rather than surfaced as its own row -- the slug position of the
    tag holds the phase, which is never a lane."""
    refs = [
        _Ref("opencode:ses_op_only", mtime=500.0, project="somewhere"),
        _Ref("opencode:ses_plain", mtime=200.0),
    ]
    transcripts = {
        "opencode:ses_op_only": (
            "<meta>OPERATOR SESSION plan</meta>\n## USER\noperator chat body\n"
        ),
        "opencode:ses_plain": "a plain chat about refactors",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    html = resp.text
    assert "operator chat body" not in html
    assert "ses_op_only" not in html
    # The plain other chat still renders.
    assert 'data-href="/chat/opencode/ses_plain"' in html
    assert "a plain chat about refactors" in html


def test_search_results_recovers_workspace_dir_from_the_provider_store(
    monkeypatch, workspace: Path, tmp_path: Path
):
    """Real refs carry a bare ``project`` basename, so without the absolute-dir
    recovery (from the provider's own store) no task row can resolve its lane
    -- which is what made every task fall back to a plain chat row."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "recovered-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal for recovered lane\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps({"slug": "recovered-lane", "status": "continue"}),
        encoding="utf-8",
    )
    (lane / "binding.json").write_text(
        json.dumps(
            {"v": 1, "root_uuid": _MARKER_ROOT_UUID, "generations": []}
        ),
        encoding="utf-8",
    )

    # A claude_code store: the recovery reads the conversation's cwd from it,
    # and the ref's own ``project`` is nothing but a basename.
    store = tmp_path / "sess_recovered.jsonl"
    store.write_text(
        json.dumps({"type": "user", "cwd": str(workspace)}) + "\n", encoding="utf-8"
    )
    refs = [_Ref("claude_code:sess_recovered", mtime=400.0, locator=str(store))]
    transcripts = {"claude_code:sess_recovered": f"{_MARKER}\n## USER\nmanaged body\n"}
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    assert f'data-href="/ws/{ws_id}/tasks/recovered-lane"' in resp.text
    assert "goal for recovered lane" in resp.text
    assert '<span class="badge" data-status="continue">in progress</span>' in resp.text


def test_chat_fork_form_carries_back_nav_hidden_inputs(monkeypatch):
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )

    with _client() as client:
        resp = client.get("/chat/cline/sess_fork")

    assert _hidden_input(resp.text, "back_url") == "/chat/cline/sess_fork"
    assert _hidden_input(resp.text, "back_label") == "Workspaces"


def test_prompt_excerpt_strips_indented_quotes_and_chat_header(monkeypatch):
    """Goal 6 / plan edge cases: the blockquote marker is dropped even when the
    line is space- or tab-indented before the ``>``, and the same stripped
    string reaches the chat page's sticky header (the body keeps its escaping)."""
    excerpt = chat_search_service._prompt_excerpt

    assert excerpt("## USER\n   > indented prompt") == "indented prompt"
    assert excerpt("## USER\n\t> tabbed prompt") == "tabbed prompt"

    from backseat_harness import history

    transcript = "## USER\n> quoted header line\n## ASSISTANT\n> reply"
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: transcript for k in keys},
    )

    with _client() as client:
        resp = client.get("/chat/opencode/ses_quoted_header")

    assert resp.status_code == 200
    header = resp.text[resp.text.index('<header class="chat-header') : resp.text.index("</header>")]
    assert "<strong>quoted header line</strong>" in header
    assert "&gt;" not in header


def test_action_forms_carry_shared_offset_class(monkeypatch, workspace: Path):
    """Goal 13 / plan 3a: the shared ``.chat-continue-form`` offset rule applies
    to the real controls -- the task page's Takeover form and the chat page's
    Continue form render with that class inside ``<header class="chat-header">``."""
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "offset-lane", updated_at=1_700_000_000.0)

    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "a chat body" for k in keys},
    )

    with _client() as client:
        task = client.get(f"/ws/{ws_id}/tasks/offset-lane")
        chat = client.get("/chat/opencode/ses_offset")

    assert task.status_code == 200 and chat.status_code == 200

    # The shared offset rule is served with the pages...
    assert "align-self: flex-end" in task.text
    assert "margin: 0.6rem 0 0" in task.text

    # ...and both real controls carry the class inside the sticky header.
    for html, form in (
        (task.text, 'id="takeover-form" class="chat-continue-form"'),
        (chat.text, 'id="resume-form" class="chat-continue-form"'),
    ):
        header = html[html.index('<header class="chat-header') : html.index("</header>")]
        assert form in header


# --- step 6c: task status derivation ----------------------------------------


def _lane_badge(workspace: Path, slug: str) -> tuple[str, str]:
    """(status, badge) the single derivation reports for one lane."""
    info = next(i for i in task_service.list_tasks(workspace) if i.slug == slug)
    return info.status, info.badge


def test_status_no_state_json_is_pending_on_every_surface(workspace: Path):
    """A lane with no state.json has never run: it reads ``pending`` in the
    listing, on the task page, and in the Run verb -- not the ``continue``
    default ``storage.load_state`` falls back to (6c)."""
    _write_lane(workspace, "fresh-lane", status=None, updated_at=1_700_000_000.0)
    ws_id = _register_workspace(workspace)

    assert _lane_badge(workspace, "fresh-lane") == ("pending", "pending")
    detail = task_service.load_task(workspace, "fresh-lane")
    assert detail is not None
    assert (detail.status, detail.badge, detail.run_action) == ("pending", "pending", "Run")

    with _client() as client:
        task_page = client.get(f"/ws/{ws_id}/tasks/fresh-lane")
    assert '<span class="badge" data-status="pending">pending</span>' in task_page.text
    assert '">in progress</span>' not in task_page.text


def test_status_continue_is_in_progress(workspace: Path):
    """Only a state.json saying ``continue`` reads as "in progress"."""
    _write_lane(
        workspace, "running-lane", status="continue", updated_at=1_700_000_000.0
    )
    ws_id = _register_workspace(workspace)

    assert _lane_badge(workspace, "running-lane") == ("continue", "in progress")
    detail = task_service.load_task(workspace, "running-lane")
    assert detail is not None
    assert (detail.status, detail.badge) == ("continue", "in progress")

    with _client() as client:
        task_page = client.get(f"/ws/{ws_id}/tasks/running-lane")
    assert '<span class="badge" data-status="continue">in progress</span>' in task_page.text


@pytest.mark.parametrize(
    ("status", "badge"),
    [("blocked", "needs attention"), ("complete", "complete")],
)
def test_status_other_states_keep_their_own_label(workspace: Path, status, badge):
    """Any state.json status other than ``continue`` keeps its own label and
    never reads as "in progress"."""
    _write_lane(workspace, "other-lane", status=status, updated_at=1_700_000_000.0)
    ws_id = _register_workspace(workspace)

    assert _lane_badge(workspace, "other-lane") == (status, badge)
    detail = task_service.load_task(workspace, "other-lane")
    assert detail is not None
    assert detail.badge == badge

    with _client() as client:
        task_page = client.get(f"/ws/{ws_id}/tasks/other-lane")
    assert f'<span class="badge" data-status="{status}">{badge}</span>' in task_page.text
    assert '">in progress</span>' not in task_page.text


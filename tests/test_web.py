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
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backseat_harness import db, marker
from backseat_harness.web.main import create_app
from backseat_harness.web.routes import shell as shell_routes
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import task as task_service
from backseat_harness.web.services import workspace as workspace_service

_PORT = 8080


def _client(host: str = "localhost") -> TestClient:
    """TestClient whose base_url makes the sent Host header allowlistable."""
    return TestClient(create_app(port=_PORT), base_url=f"http://{host}:{_PORT}")


# --- Host/Origin allowlist (step 3.2) ------------------------------------------


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_host_header_is_accepted(host):
    with _client(host) as client:
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


# --- extra Host/Origin strings (caddy mode site addresses) ---------------------

_EXTRA_HOST = "site.lan:8443"


def test_extra_host_accepted_for_http_and_origin():
    with TestClient(
        create_app(port=_PORT, extra_hosts=(_EXTRA_HOST,)),
        base_url=f"http://{_EXTRA_HOST}",
    ) as client:
        assert client.get("/").status_code == 200
        resp = client.post(
            "/workspaces",
            data={"path": "/nowhere"},
            headers={"Origin": f"https://{_EXTRA_HOST}"},
        )
        assert resp.status_code != 403
        assert "forbidden origin" not in resp.text

        # A foreign Origin is still refused on the extra-host site.
        foreign_origin = client.post(
            "/workspaces",
            data={"path": "/nowhere"},
            headers={"Origin": "http://evil.com"},
        )
        assert foreign_origin.status_code == 403
        assert "forbidden origin" in foreign_origin.text

        foreign = client.get("/", headers={"Host": "evil.com"})
    assert foreign.status_code == 403
    assert "forbidden host" in foreign.text


def test_portless_extra_host_forms_are_accepted():
    """Browsers leave :443 out of Host and Origin, so both forms of an https site pass."""
    hosts = ("site.lan", "site.lan:443")
    with TestClient(
        create_app(port=_PORT, extra_hosts=hosts), base_url="https://site.lan"
    ) as client:
        assert client.get("/").status_code == 200
        assert client.get("/", headers={"Origin": "https://site.lan"}).status_code == 200
        assert client.get("/", headers={"Origin": "https://site.lan:443"}).status_code == 200


@pytest.mark.parametrize(
    ("extra_hosts", "netloc", "origin"),
    [
        ((), f"localhost:{_PORT}", f"http://localhost:{_PORT}"),
        ((_EXTRA_HOST,), _EXTRA_HOST, f"https://{_EXTRA_HOST}"),
    ],
    ids=["localhost", "extra-host"],
)
def test_shell_websocket_accepts_allowed_origin_rejects_foreign(
    tmp_path: Path, extra_hosts, netloc, origin
):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with TestClient(
        create_app(port=_PORT, extra_hosts=extra_hosts),
        base_url=f"http://{netloc}",
    ) as client:
        session_id = _start_session(client, cmd, tmp_path)
        url = f"ws://{netloc}/ws/shell/{session_id}"
        try:
            # Positive control: the allowed Origin connects.
            with client.websocket_connect(url, headers={"origin": origin}):
                pass

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(url, headers={"origin": "http://evil.com"}):
                    pass

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(url, headers={"host": "evil.com"}):
                    pass
        finally:
            _kill_session(session_id)


def test_runners_pass_extra_hosts_to_create_app(monkeypatch):
    """run_web and start_web_in_thread both hand extra_hosts to create_app."""
    import uvicorn

    from backseat_harness.web import cli as web_cli
    from backseat_harness.web import main as web_main

    seen: list[tuple[str, ...]] = []
    real_create_app = web_main.create_app

    def spy(port: int = 8090, extra_hosts=()):
        seen.append(tuple(extra_hosts))
        return real_create_app(port=port, extra_hosts=extra_hosts)

    class _StubServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(web_main, "create_app", spy)
    monkeypatch.setattr(uvicorn, "Server", _StubServer)

    web_cli.run_web(port=0, extra_hosts=[_EXTRA_HOST])
    web_cli.start_web_in_thread(port=0, extra_hosts=[_EXTRA_HOST]).stop()

    assert seen == [(_EXTRA_HOST,), (_EXTRA_HOST,)]


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
    - ``load_sessions`` (batched transcripts) for the rows' text,
    - ``fork_session`` (the fork route's rendering) for ``/search/fork``.

    ``load_chat`` is deliberately *not* stubbed: a request for it means some
    path went back to the per-row tiered search that re-scans every stored
    conversation for each row, which is what made ``/ws/<id>`` take minutes
    (history.load_sessions).

    Returns the recorded ``fork_session`` calls as ``(search, history_provider)``.
    A fork returns ``transcripts["<provider>:<conversation id>"]`` or "".
    """
    from backseat_harness import history

    def _fake_load(attr: str):
        if attr == "find_chats":
            return lambda query="", providers=None, limit=None: list(refs)
        if attr == "default_providers":
            return lambda: []
        raise AssertionError(f"unexpected history seam requested: {attr}")

    forks: list[tuple[str, str]] = []

    def _fake_fork(search: str, history_provider: str) -> str:
        forks.append((search, history_provider))
        return transcripts.get(f"{history_provider}:{search}", "")

    monkeypatch.setattr(history, "_load", _fake_load)
    monkeypatch.setattr(history, "list_session_refs", lambda limit=None: list(refs))
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: transcripts[k] for k in keys if k in transcripts},
    )
    monkeypatch.setattr(history, "fork_session", _fake_fork)
    return forks


def _data_rows(html: str) -> list[str]:
    """Body rows of a table (header rows use <th>, not <td>); rows may carry
    attributes such as the shared style's ``data-href``."""
    return [
        row
        for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.S)
        if "<td>" in row
    ]


def test_pick_fork_provider_supported_source_keeps_its_own_provider(monkeypatch):
    # Rule 1 fires before the configured provider and the PATH check, so no CLI
    # needs to be installed for a supported source.
    monkeypatch.setattr("shutil.which", lambda exe: None)

    assert chat_search_service.pick_fork_provider("opencode:ses_1", "claude") == "opencode"
    assert chat_search_service.pick_fork_provider("kiro_cli:ses_1", "claude") == "kiro"
    assert chat_search_service.pick_fork_provider("claude_code:ses_1", "kiro") == "claude"


def test_pick_fork_provider_kiro_ide_maps_to_kiro(monkeypatch):
    # Rule 2 fires before the configured provider, so nothing needs to be installed.
    monkeypatch.setattr("shutil.which", lambda exe: None)

    assert chat_search_service.pick_fork_provider("kiro_ide:sess_1", "claude") == "kiro"
    assert chat_search_service.pick_fork_provider("kiro_ide_v2:sess_1", "opencode") == "kiro"


def test_pick_fork_provider_other_source_uses_configured_provider(monkeypatch):
    # kilo / cline / unmapped sources have no fork provider of their own (rule 3).
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)

    for source in ("kilo:sess_1", "cline:sess_1", "unknown:sess_1"):
        assert chat_search_service.pick_fork_provider(source, "claude") == "claude"
        assert chat_search_service.pick_fork_provider(source, "opencode") == "opencode"


def test_pick_fork_provider_ignores_recent_chats_and_path_scan_order(monkeypatch):
    # Every CLI is installed, so the retired opencode-first PATH scan would have
    # answered "opencode"; the configured provider wins instead.
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)

    assert chat_search_service.pick_fork_provider("kilo:sess_1", "kiro") == "kiro"

    # The candidate list is gone from the signature, so no recent chat can pick.
    import inspect

    params = list(inspect.signature(chat_search_service.pick_fork_provider).parameters)
    assert params == ["current_session_key", "configured_provider"]


@pytest.mark.parametrize("configured", ["bogus", "kilo", "kiro-cli", ""])
def test_pick_fork_provider_guard_rejects_unsupported_configured(monkeypatch, configured):
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)

    with pytest.raises(RuntimeError, match="cannot fork a chat"):
        chat_search_service.pick_fork_provider("kilo:sess_1", configured)


@pytest.mark.parametrize(
    ("configured", "binary"),
    [("claude", "claude"), ("kiro", "kiro-cli"), ("opencode", "opencode")],
)
def test_pick_fork_provider_guard_rejects_configured_cli_not_on_path(
    monkeypatch, configured, binary
):
    monkeypatch.setattr("shutil.which", lambda exe: None)

    with pytest.raises(RuntimeError, match=f"no CLI on PATH \\(looked for {binary}\\)"):
        chat_search_service.pick_fork_provider("kilo:sess_1", configured)


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


def test_shell_start_accepts_initial_pty_size(tmp_path: Path):
    """POST /shell/start with optional cols/rows sizes the PTY at spawn
    (plan 3g): a real child reports the OS-view of its own terminal."""
    probe = "COLSROWS_OK"
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys, time; "
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


# --- stylesheet contract -------------------------------------------------------

#: (rule marker, property/value fragments that must appear in that rule). The
#: served CSS is one shared stylesheet, so one page fetch covers every rule.
#: Pixel sizes and rule order are deliberately not pinned; a fragment with no
#: text to check (``[]``) only asserts the rule is present.
_STYLE_CONTRACT = [
    (".chat-header-prompt > strong {", ["overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"]),
    (".chat-header .chat-continue-form {", ["align-self: flex-end", "margin: 0.6rem 0 0"]),
    (".transcript {", ["overflow: auto", "overscroll-behavior: auto"]),
    (".chat-header-back {", ["text-decoration: none"]),
    (".expander-plus {", ["display: inline-flex", "place-items: center", "padding: 0"]),
    (".expander-plus:hover {", ["background: var(--surface-hover)"]),
    (".expander[open] .expander-plus {", ["background: var(--surface-hover)"]),
    (".expander-plus::before,", ["position: absolute", "top: 50%", "left: 50%", "translate(-50%, -50%)"]),
    ("transform: translate(-50%, -50%) scaleY(0)", []),
    ("textarea {", ["font-size: 0.85rem"]),
    (".data-table .prompt h4 {", ["margin: 0 0 0.3rem", "0.72rem", "font-weight: 700"]),
    (".data-table small,", [".prompt", "font-size: 0.8em"]),
    (".data-table a {", ["text-decoration: none"]),
    (
        ".data-table tbody tr:not(.empty-row):not(:has(a.task-link:hover)):hover a:not(.task-link) {",
        ["text-decoration: underline", "color: var(--accent-hover)"],
    ),
    (".data-table a.task-link:hover {", ["text-decoration: underline", "color: var(--accent-hover)"]),
]


@pytest.mark.parametrize(("marker", "fragments"), _STYLE_CONTRACT, ids=lambda v: str(v)[:40])
def test_stylesheet_contract(marker, fragments):
    """Each rule keeps the property/value pairs that carry its behaviour."""
    with _client() as client:
        css = client.get("/").text

    assert marker in css
    start = css.index(marker)
    body = css[start : css.index("}", start)]
    for fragment in fragments:
        assert fragment in body


def test_base_has_no_global_h1_and_smaller_font():
    """Goal G4: the redundant site-title header is gone and the base font is
    14px (plan 3c)."""
    with _client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert "backseat-harness</h1>" not in resp.text
    assert 'class="nav-back"' not in resp.text


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
    header-less Provider/Iteration/Generation/Reason table, a required-brief
    Run block with the in progress copy beside a 2/3 Status column,
    state-colored checklist capsules, no Logs section and no inline
    transcript -- just a "Raw transcript" link to the 6d page."""
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
    (lane / "binding.json").write_text(
        json.dumps(
            {
                "v": 1,
                "root_uuid": "11111111-2222-3333-4444-555555555555",
                "provider": "claude",
                "generations": [],
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

    # Provider / Iteration / Generation / Reason: a header-less table, with the
    # lane's binding provider first.
    meta_start = html.index('<table class="task-meta">')
    meta = html[meta_start : html.index("</table>", meta_start)]
    assert "<thead>" not in meta
    assert "<td>Provider</td>" in meta and "<td>claude</td>" in meta
    assert meta.index("<td>Provider</td>") < meta.index("<td>Iteration</td>")
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


def test_task_page_provider_row_falls_back_to_the_configured_provider(
    workspace: Path,
):
    """A never-run lane has no binding.json, so the Status table's Provider
    row reads the workspace's configured [harness] provider -- and the plain
    manager default when nothing configures one."""
    (workspace / ".backseat" / "config.toml").write_text(
        '[harness]\nprovider = "kiro"\n', encoding="utf-8"
    )
    ws_id = _register_workspace(workspace)
    _write_lane(workspace, "fresh-lane", status="continue", updated_at=1_700_000_000.0)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/fresh-lane")

    assert page.status_code == 200
    meta_start = page.text.index('<table class="task-meta">')
    meta = page.text[meta_start : page.text.index("</table>", meta_start)]
    assert "<td>Provider</td>" in meta and "<td>kiro</td>" in meta

    # With no binding and no workspace config, the manager default applies.
    (workspace / ".backseat" / "config.toml").unlink()
    _write_lane(workspace, "default-lane", status="continue", updated_at=1_700_000_000.0)

    with _client() as client:
        page = client.get(f"/ws/{ws_id}/tasks/default-lane")

    meta_start = page.text.index('<table class="task-meta">')
    meta = page.text[meta_start : page.text.index("</table>", meta_start)]
    assert "<td>Provider</td>" in meta and "<td>opencode</td>" in meta


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


_MARKED_TAG = '<script src="https://cdn.jsdelivr.net/npm/marked@18.1.1/lib/marked.umd.min.js"></script>'
_DOMPURIFY_TAG = '<script src="https://cdn.jsdelivr.net/npm/dompurify@3.4.17/dist/purify.min.js"></script>'
_MD_PAYLOAD = "## USER\n> <script>alert(1)</script> & <b>bold</b>\n"


def _assert_transcript_markdown_page(html: str) -> None:
    """Both transcript pages: pinned renderer tags, the escaped <pre> fallback,
    and the renderer's ``transcript-md`` swap."""
    assert _MARKED_TAG in html
    assert _DOMPURIFY_TAG in html
    # The server still emits the escaped plain-text <pre> the renderer falls back to.
    assert '<pre class="transcript">' in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "transcript-md" in html
    # The renderer escapes the source's raw HTML before marked parses it: a
    # transcript quoting an HTML diff (an unclosed <!-- or a stray <pre>)
    # must not be able to truncate or reshape the rendered box.
    assert ".replace(/&/g, \"&amp;\")" in html
    assert ".replace(/</g, \"&lt;\")" in html
    assert "md.parse(source" in html


def test_chat_page_includes_transcript_markdown_renderer(monkeypatch):
    """The chat page loads the pinned marked and DOMPurify builds and carries
    the renderer that swaps the transcript <pre> for sanitized markdown."""
    from backseat_harness import history

    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: _MD_PAYLOAD for k in keys}
    )
    with _client() as client:
        resp = client.get("/chat/opencode/ses_md")

    assert resp.status_code == 200
    _assert_transcript_markdown_page(resp.text)


def test_task_transcript_page_includes_transcript_markdown_renderer(workspace: Path):
    """The task transcript page loads the same renderer and fallback <pre>."""
    ws_id = _register_workspace(workspace)
    _write_transcript_lane(workspace, "md-lane", _MD_PAYLOAD)

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}/tasks/md-lane/transcript")

    assert resp.status_code == 200
    _assert_transcript_markdown_page(resp.text)


def test_task_page_renders_goal_as_markdown(workspace: Path):
    """The task page's goal gets the same renderer, scoped to the goal <pre>,
    with the escaped <pre> kept as the fallback and prose styling instead of
    the transcript's monospace panel."""
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "goal-md-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("## Ship it\n\n- <b>one</b>\n- two\n", encoding="utf-8")

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}/tasks/goal-md-lane")

    assert resp.status_code == 200
    html = resp.text
    assert _MARKED_TAG in html and _DOMPURIFY_TAG in html
    assert '<pre class="goal">## Ship it' in html
    assert "document.querySelector(\"pre.goal\")" in html
    assert '"goal goal-md"' in html
    # The fallback <pre> keeps the goal escaped, so raw HTML in it never reaches the page.
    assert "- &lt;b&gt;one&lt;/b&gt;" in html
    assert "- <b>one</b>" not in html


def test_workspace_page_loads_no_transcript_renderer(workspace: Path, monkeypatch):
    """Only the chat and task transcript pages load marked and DOMPurify; the
    workspace page has no transcript box and must not pull them in."""
    ws_id = _register_workspace(workspace)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    assert "marked.umd" not in resp.text
    assert "dompurify" not in resp.text
    # base.html's .transcript-md CSS is always served, so check the renderer's code.
    assert "marked.parse" not in resp.text
    assert "DOMPurify.sanitize" not in resp.text


def test_base_css_has_transcript_markdown_rules():
    """The rendered-transcript panel styles ship in the shared base stylesheet,
    beside the unchanged ``.transcript`` rule."""
    with _client() as client:
        css = client.get("/").text

    assert ".transcript-md {" in css
    assert ".transcript-md pre {" in css


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


def test_completed_tasks_and_other_chats_render_their_empty_states(
    monkeypatch, workspace: Path
):
    """With no tasks and no chats, each section keeps its heading and shows its
    empty-state text with no data rows (plan edge case)."""
    _register_workspace(workspace)
    _stub_chat_history(monkeypatch, [], {})

    with _client() as client:
        resp = client.get("/ws/1")

    assert resp.status_code == 200
    completed = _section(resp.text, "<h4>Completed tasks</h4>")
    assert "No completed tasks." in completed
    assert _data_rows(completed) == []
    chats = _other_chats_section(resp.text)
    assert "No other chats." in chats
    assert _data_rows(chats) == []


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
    assert ">Resume<" in resp.text
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
    assert ">Resume<" in resp.text
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
    assert ">Resume<" not in resp.text
    assert 'action="/search/fork"' in resp.text
    assert _hidden_input(resp.text, "conversation_id") == "sess_fork"
    assert _hidden_input(resp.text, "current_session_key") == "cline:sess_fork"
    assert ">Fork<" in resp.text


def _fork_form_html(html: str) -> str:
    """The fork form's markup alone: the page's generate-task form also carries a
    ``ws`` input, so a whole-page check cannot tell the two apart."""
    start = html.index('id="fork-form"')
    return html[start : html.index("</form>", start)]


def test_chat_page_fork_form_posts_the_bound_workspace_id(monkeypatch, workspace: Path):
    from backseat_harness import history

    ws_id = _register_workspace(workspace)
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )
    with _client() as client:
        resp = client.get(f"/chat/cline/sess_fork?ws={ws_id}")

    form = _fork_form_html(resp.text)
    assert f'<input type="hidden" name="ws" value="{ws_id}">' in form


def test_chat_page_fork_form_omits_ws_without_a_bound_workspace(monkeypatch):
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )
    with _client() as client:
        resp = client.get("/chat/cline/sess_fork")

    assert 'name="ws"' not in _fork_form_html(resp.text)


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


_RECAP_INSTRUCTION = (
    "Then give a very brief recap of the conversation, and ask the user what "
    "they want to do next."
)


def test_fork_prompt_names_source_session_and_file_and_ends_with_recap_instruction(
    tmp_path,
):
    """The prompt names the source provider and session and the transcript file
    to read first, and ends with the recap-then-ask instruction."""
    from backseat_harness.web.routes import search as search_routes

    transcript = tmp_path / "transcript-sess_42.txt"
    prompt = search_routes._fork_prompt("kilo", "sess_42", transcript)

    assert prompt.startswith("This chat picks up from kilo chat session sess_42.")
    assert f"Read the transcript file {transcript} first" in prompt
    assert prompt.endswith(_RECAP_INSTRUCTION)
    assert prompt.index(str(transcript)) < prompt.index("recap")
    # It is a read-this-file request: no sentence may ask the CLI to fork
    # (the path is checked apart from the text, since tmp_path names this test).
    assert "fork" not in prompt.replace(str(transcript), "").lower()


def _capture_fork_command(monkeypatch, chat_root: Path) -> dict:
    """Capture the argv and cwd the fork route hands to the PTY manager -- the
    real launch command -- without spawning a child. The server's cwd is moved
    to ``chat_root`` so a route call without ``?ws=`` writes under it, never into
    the repo."""
    from backseat_harness.web.shell.manager import PTYSessionManager

    captured: dict[str, object] = {}

    def _fake_create_session(self, cmd, cwd, cols=None, rows=None):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return "abc123def456"

    monkeypatch.chdir(chat_root)
    monkeypatch.setattr(PTYSessionManager, "create_session", _fake_create_session)
    return captured


def _post_fork(conversation_id: str, key: str, **extra) -> object:
    with _client() as client:
        return client.post(
            "/search/fork",
            data={"conversation_id": conversation_id, "current_session_key": key, **extra},
            follow_redirects=False,
        )


def _assert_fork_prompt(cmd: list[str], transcript: Path, source: str, session: str) -> None:
    """argv carries the prompt last: source, session, file path, then the instruction."""
    prompt = cmd[-1]
    assert f"This chat picks up from {source} chat session {session}." in prompt
    assert str(transcript.resolve()) in prompt
    assert prompt.index(str(transcript.resolve())) < prompt.index("recap")
    assert prompt.endswith(_RECAP_INSTRUCTION)


@pytest.mark.parametrize(
    ("key", "source", "launch"),
    [
        # kiro_ide forks via kiro-cli; a kilo chat has no fork provider of its
        # own, so it forks via the configured [harness] provider (opencode).
        ("kiro_ide:sess_cur", "kiro_ide", ["kiro-cli", "chat"]),
        ("kilo:sess_k", "kilo", ["opencode", "--prompt"]),
    ],
    ids=["kiro_ide", "kilo"],
)
def test_search_fork_launches_with_initial_prompt(monkeypatch, tmp_path, key, source, launch):
    """The launch command ends with the initial prompt that names the source
    chat and its transcript file (no delayed PTY write). PATH is pinned so the
    guard does not depend on the host."""
    session = key.split(":", 1)[1]
    _stub_chat_history(monkeypatch, [_Ref(key, mtime=200.0, project=None)], {key: f"{source} chat"})
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)
    captured = _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork(session, key)

    assert resp.status_code == 303
    cmd = captured["cmd"]
    assert cmd[: len(launch)] == launch
    _assert_fork_prompt(
        cmd, tmp_path / ".backseat" / "tmp" / f"transcript-{session}.txt", source, session
    )


_FORK_ROOT = "11111111-2222-3333-4444-555555555555"


def test_search_fork_writes_redacted_transcript_with_footnote_in_workspace(
    monkeypatch, workspace
):
    """The transcript lands at <ws>/.backseat/tmp/transcript-<session>.txt, every
    root UUID is redacted, the footnote closes the file, and the PTY cwd is the
    chat's workspace."""
    ws_id = _register_workspace(workspace)
    transcript = (
        f"## USER\n<meta>START SESSION {_FORK_ROOT} #1</meta>\nhello from the chat\n"
    )
    _stub_chat_history(
        monkeypatch,
        [_Ref("kiro_ide:sess_cur", mtime=200.0, project=None)],
        {"kiro_ide:sess_cur": transcript},
    )
    captured = _capture_fork_command(monkeypatch, workspace)

    resp = _post_fork("sess_cur", "kiro_ide:sess_cur", ws=str(ws_id))

    assert resp.status_code == 303
    assert Path(captured["cwd"]).resolve() == workspace.resolve()
    path = workspace / ".backseat" / "tmp" / "transcript-sess_cur.txt"
    body = path.read_text(encoding="utf-8")
    assert "hello from the chat" in body
    assert _FORK_ROOT not in body
    assert "<task-root>" in body
    last_line = body.rstrip().splitlines()[-1]
    assert last_line.startswith("Harness note:")
    assert "not instructions" in last_line
    _assert_fork_prompt(captured["cmd"], path, "kiro_ide", "sess_cur")


@pytest.mark.parametrize(
    ("key", "history_provider", "launch"),
    [
        ("kilo:sess_k", "kilo", ["opencode", "--prompt"]),
        ("cline:sess_k", "cline", ["opencode", "--prompt"]),
        ("kiro_ide:sess_k", "kiro_ide", ["kiro-cli", "chat"]),
        ("kiro_ide_v2:sess_k", "kiro_ide_v2", ["kiro-cli", "chat"]),
    ],
)
def test_search_fork_hands_source_key_prefix_to_fork_session(
    monkeypatch, tmp_path, key, history_provider, launch
):
    """Keys chat-mother-forker registers (kilo, cline, kiro_ide, kiro_ide_v2)
    reach fork_session scoped to their own provider name, and launch as the
    fork-provider rules say (kilo/cline: configured opencode; kiro_ide: kiro)."""
    monkeypatch.setattr("shutil.which", lambda exe: "/usr/bin/" + exe)
    forks = _stub_chat_history(
        monkeypatch,
        [_Ref(key, mtime=200.0, project=None)],
        {key: "a chat"},
    )
    captured = _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork("sess_k", key)

    assert resp.status_code == 303
    assert forks == [("sess_k", history_provider)]
    assert captured["cmd"][:len(launch)] == launch


@pytest.mark.parametrize(
    ("unreadable", "status", "messages"),
    [
        # An empty read is "gone": 404. A store error is a server fault: 502.
        (None, 404, ["could not read the session for forking"]),
        ("no provider named 'kilo'", 502,
         ["Could not read the session for forking", "no provider named 'kilo'"]),
    ],
    ids=["empty-transcript", "history-error"],
)
def test_search_fork_unreadable_session_answers_an_error_and_launches_nothing(
    monkeypatch, tmp_path, unreadable, status, messages
):
    from backseat_harness import history

    _stub_chat_history(monkeypatch, [], {})
    if unreadable is not None:

        def _unknown_provider(search, provider):
            raise history.HistoryError(f"chat-mother-forker has {unreadable}")

        monkeypatch.setattr(history, "fork_session", _unknown_provider)
    captured = _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork("sess_k", "kilo:sess_k")

    assert resp.status_code == status
    for message in messages:
        assert message in resp.text
    assert captured == {}
    assert not (tmp_path / ".backseat" / "tmp").exists()


def test_search_fork_refork_overwrites_the_transcript(monkeypatch, tmp_path):
    from backseat_harness import history

    _stub_chat_history(monkeypatch, [], {})
    outputs = iter(["first copy of the chat", "second copy of the chat"])
    monkeypatch.setattr(history, "fork_session", lambda search, provider: next(outputs))
    _capture_fork_command(monkeypatch, tmp_path)

    _post_fork("sess_k", "kiro_ide:sess_k")
    _post_fork("sess_k", "kiro_ide:sess_k")

    body = (tmp_path / ".backseat" / "tmp" / "transcript-sess_k.txt").read_text(
        encoding="utf-8"
    )
    assert "second copy of the chat" in body
    assert "first copy of the chat" not in body


def test_search_fork_sanitizes_the_session_id_in_the_file_name(monkeypatch, tmp_path):
    """A session id with path separators cannot escape the tmp dir: unsafe
    characters become underscores in the file name."""
    key = "kiro_ide:sess/../x"
    _stub_chat_history(monkeypatch, [], {key: "a chat"})
    captured = _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork("sess/../x", key)

    assert resp.status_code == 303
    tmp_dir = tmp_path / ".backseat" / "tmp"
    assert [p.name for p in tmp_dir.iterdir()] == ["transcript-sess_.._x.txt"]
    assert str((tmp_dir / "transcript-sess_.._x.txt").resolve()) in captured["cmd"][-1]


@pytest.mark.parametrize(
    "extra",
    [{}, {"ws": ""}, {"ws": "999999"}],
    ids=["no-ws", "empty-ws", "unknown-ws"],
)
def test_search_fork_without_a_workspace_id_posts_cleanly_to_the_server_cwd(
    monkeypatch, tmp_path, extra
):
    """A fork with no bound workspace (the form omits ``ws``, sends it empty, or
    names an unregistered id) is accepted, not a 422, and uses the server's cwd."""
    _stub_chat_history(monkeypatch, [], {"kiro_ide:sess_k": "a chat"})
    captured = _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork("sess_k", "kiro_ide:sess_k", **extra)

    assert resp.status_code == 303
    assert captured["cwd"] == Path(".")
    assert (tmp_path / ".backseat" / "tmp" / "transcript-sess_k.txt").is_file()


@pytest.mark.parametrize(
    ("configured", "which_missing", "message"),
    [
        ("codex", None, "cannot fork a chat"),
        ("claude", "claude", "has no CLI on PATH"),
    ],
    ids=["unsupported-provider", "cli-not-on-path"],
)
def test_search_fork_guard_refusal_is_a_readable_500_and_launches_nothing(
    monkeypatch, workspace, configured, which_missing, message
):
    """A non-resumable source (kilo) forks via the configured [harness] provider.
    When that provider is refused by pick_fork_provider's guard, the route answers
    a readable 500 with the guard's message, creates no PTY session and writes no
    transcript -- not a bare Starlette 500."""
    ws_id = _register_workspace(workspace)
    (workspace / ".backseat" / "config.toml").write_text(
        f'[harness]\nprovider = "{configured}"\n', encoding="utf-8"
    )
    _stub_chat_history(monkeypatch, [], {"kilo:sess_k": "a kilo chat"})
    monkeypatch.setattr(
        "shutil.which",
        lambda exe: None if exe == which_missing else "/usr/bin/" + exe,
    )
    captured = _capture_fork_command(monkeypatch, workspace)

    resp = _post_fork("sess_k", "kilo:sess_k", ws=str(ws_id))

    assert resp.status_code == 500
    assert message in resp.text
    assert captured == {}
    assert not (workspace / ".backseat" / "tmp").exists()


def test_search_fork_store_read_runs_off_the_event_loop(monkeypatch, tmp_path):
    """The store read is blocking, so it must run in a worker thread: an asyncio
    loop is not running in the thread that calls fork_session."""
    import asyncio

    from backseat_harness import history

    on_loop: list[bool] = []

    def _fake_fork(search, provider):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return "a kilo chat"

    _stub_chat_history(monkeypatch, [], {})
    monkeypatch.setattr(history, "fork_session", _fake_fork)
    _capture_fork_command(monkeypatch, tmp_path)

    resp = _post_fork("sess_k", "kilo:sess_k")

    assert resp.status_code == 303
    assert on_loop == [False]


# --- step 3a: shared polish ----------------------------------------------------


def test_data_table_rows_hover_and_links_underline_only_on_hover():
    """Goal 4: populated rows highlight; in-table links stay quiet (no
    underline) until their row is hovered, then underline and accent-light."""
    with _client() as client:
        page = client.get("/")

    assert page.status_code == 200
    css = page.text
    assert ".data-table tbody tr:not(.empty-row):hover" in css


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


@pytest.mark.parametrize(
    ("params", "headers"),
    [
        (None, {"referer": "http://evil.com/chat/opencode/ses_x"}),
        ({"from": "//evil.com/x", "label": "x"}, None),
        (None, {"referer": f"http://localhost:{_PORT}//evil.example/x?ws=3"}),
        # A browser reads a backslash as "/" and drops tab/CR/LF, so these
        # values become protocol-relative: each must fall back to the index.
        ({"from": "//evil.example/x", "label": "x"}, None),
        ({"from": "/\\evil.example/x", "label": "x"}, None),
        ({"from": "/\\\\evil.example/x", "label": "x"}, None),
        ({"from": "/\t/evil.example/x", "label": "x"}, None),
        ({"from": "/chat/opencode/ses_x\r\nLocation: evil", "label": "x"}, None),
    ],
)
def test_shell_page_back_link_rejects_untrusted_from_or_referer(tmp_path: Path, params, headers):
    """A foreign or protocol-relative Referer, or a rewritten ``from``, falls
    back to the workspaces index and never becomes a protocol-relative link."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(f"/shell/{session_id}", params=params, headers=headers)
            assert page.status_code == 200
            assert '<a class="chat-header-back" href="/">← Workspaces</a>' in page.text
            assert 'href="//evil' not in page.text
        finally:
            _kill_session(session_id)


@pytest.mark.parametrize(
    ("session", "data"),
    [
        ("live", {"back_url": "/\\evil.example/x"}),
        ("live", {"back_url": "https://evil.com/"}),
        ("unknown", {}),
    ],
    ids=["backslash-back-url", "foreign-back-url", "unknown-session"],
)
def test_shell_stop_unsafe_or_unknown_session_redirects_home(tmp_path: Path, session, data):
    """shell_stop shares the back_url check, and an unknown session still redirects."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path) if session == "live" else "000000000000"
        resp = client.post(f"/shell/{session_id}/stop", data=data, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


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


@pytest.mark.parametrize(
    "path",
    ["/chat/opencode/ses_backnav", "/chat/cline/sess_fork"],
    ids=["continue", "fork"],
)
def test_chat_pages_carry_back_nav_hidden_inputs(monkeypatch, path):
    """The continue and fork forms both send the chat itself as back_url, labelled Workspaces."""
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "chat body" for k in keys},
    )

    with _client() as client:
        resp = client.get(path)

    assert _hidden_input(resp.text, "back_url") == path
    assert _hidden_input(resp.text, "back_label") == "Workspaces"


def test_chat_continue_form_recarries_workspace_in_back_url(
    monkeypatch, workspace: Path
):
    """Regression: the ``?ws=`` binding must survive the shell round trip.

    The chat page carries its workspace only in the query string, so a
    ``back_url`` built from the path alone sends the user back to a chat page
    with no workspace -- and its header degrades to the "Workspaces" homepage.
    """
    from backseat_harness import history

    ws_id = _register_workspace(workspace)
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: "chat body" for k in keys}
    )

    with _client() as client:
        resp = client.get(f"/chat/opencode/ses_backnav?ws={ws_id}")

    assert _hidden_input(resp.text, "back_url") == f"/chat/opencode/ses_backnav?ws={ws_id}"
    assert _hidden_input(resp.text, "back_label") == workspace.name


def test_chat_shell_round_trip_returns_to_workspace(workspace: Path, tmp_path: Path, monkeypatch):
    """Full reported flow: workspace -> chat -> Continue -> shell -> back.

    The transcript's back link must again resolve to the exact workspace, not
    the homepage.
    """
    from backseat_harness import history

    ws_id = _register_workspace(workspace)
    monkeypatch.setattr(
        history, "load_sessions", lambda keys, providers=None: {k: "chat body" for k in keys}
    )
    chat_url = f"/chat/opencode/ses_roundtrip?ws={ws_id}"

    with _client() as client:
        chat = client.get(chat_url)
        assert f'<a class="chat-header-back" href="/ws/{ws_id}">' in chat.text

        session_id = _start_session(
            client, [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path
        )
        try:
            shell = client.get(
                f"/shell/{session_id}",
                params={
                    "from": _hidden_input(chat.text, "back_url"),
                    "label": _hidden_input(chat.text, "back_label"),
                },
            )
            # Shell back link carries the workspace binding through.
            assert (
                f'<a class="chat-header-back" href="{chat_url}">← {workspace.name}</a>'
                in shell.text
            )

            # Following it lands on a transcript whose back link is the workspace.
            returned = client.get(chat_url)
            assert f'<a class="chat-header-back" href="/ws/{ws_id}">' in returned.text
        finally:
            _kill_session(session_id)


def test_shell_page_back_link_keeps_referer_query(tmp_path: Path):
    """The same-origin Referer fallback must not drop ``?ws=``."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(
                f"/shell/{session_id}",
                headers={
                    "referer": f"http://localhost:{_PORT}/chat/opencode/ses_x?ws=7",
                },
            )
            assert (
                '<a class="chat-header-back" href="/chat/opencode/ses_x?ws=7">← Chat</a>'
                in page.text
            )
        finally:
            _kill_session(session_id)


def test_search_fork_redirect_carries_back_nav_params(monkeypatch, tmp_path):
    refs = [_Ref("kiro_ide:sess_cur", mtime=200.0, project=None)]
    _stub_chat_history(monkeypatch, refs, {"kiro_ide:sess_cur": "kiro ide chat"})
    _capture_fork_command(monkeypatch, tmp_path)

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

    # The deduped task row: goal prompt under the task-name heading, status
    # capsule, task link, and the chat's own provider (never the lane slug)
    # in the Provider column.
    assert f'data-href="/ws/{ws_id}/tasks/search-lane"' in html
    assert "goal for search lane" in html
    assert '<span class="badge" data-status="blocked">needs attention</span>' in html
    # The duplicate hit was skipped: exactly one row links to the task page.
    assert html.count(f'data-href="/ws/{ws_id}/tasks/search-lane"') == 1

    def _cells(row: str) -> list[str]:
        return re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)

    task_row = next(r for r in _data_rows(html) if "search-lane" in r)
    task_cells = _cells(task_row)
    assert task_cells[0] == (
        f'<h4>search-lane</h4><a href="/ws/{ws_id}/tasks/search-lane">'
        "goal for search lane</a>"
    )
    assert task_cells[1].strip() == "opencode"

    # The other-chat row: parsed prompt, provider, plain N/A, chat link.
    assert 'data-href="/chat/opencode/ses_plain"' in html
    assert "a plain chat about refactors" in html
    assert "N/A" in html
    plain_row = next(r for r in _data_rows(html) if "ses_plain" in r)
    assert _cells(plain_row)[1].strip() == "opencode"

    # Chat-search order is preserved: the task row precedes the other row.
    assert html.index("goal for search lane") < html.index("a plain chat about refactors")

    # Both rows carry the formatted mtime.
    assert datetime.fromtimestamp(400.0).strftime("%Y-%m-%d %H:%M") in html
    assert datetime.fromtimestamp(200.0).strftime("%Y-%m-%d %H:%M") in html


@pytest.mark.parametrize(
    ("sessions", "rows", "no_match"),
    [(25, 20, False), (0, 0, True)],
    ids=["capped-at-twenty", "empty"],
)
def test_search_results_row_count_is_capped_and_empty_says_so(monkeypatch, sessions, rows, no_match):
    refs = [_Ref(f"opencode:ses_{i:02d}", mtime=100.0 + i) for i in range(sessions)]
    _stub_chat_history(monkeypatch, refs, {})

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert resp.status_code == 200
    assert resp.text.count('data-href="/chat/') == rows
    assert ("No sessions found." in resp.text) is no_match


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
    # The row is marked as a task by the heading and status capsule, while the
    # Provider column holds the first hit's chat provider.
    task_row = next(r for r in _data_rows(html) if "op-lane" in r)
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", task_row, re.S)
    assert cells[0].startswith("<h4>op-lane</h4>")
    assert cells[1].strip() == "opencode"


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
    # The Provider column shows the chat's provider, not the lane name.
    row = next(r for r in _data_rows(resp.text) if "recovered-lane" in r)
    cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
    assert cells[1].strip() == "claude_code"


def test_search_plain_chat_in_a_registered_workspace_links_with_ws(
    monkeypatch, workspace: Path
):
    ws_id = _register_workspace(workspace)
    refs = [_Ref("opencode:ses_plain", mtime=200.0, project=str(workspace))]
    _stub_chat_history(monkeypatch, refs, {"opencode:ses_plain": "a plain chat"})

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert f'data-href="/chat/opencode/ses_plain?ws={ws_id}"' in resp.text
    assert f'<a href="/chat/opencode/ses_plain?ws={ws_id}">' in resp.text


def test_search_plain_chat_in_an_unmapped_project_stays_bare(
    monkeypatch, workspace: Path, tmp_path: Path
):
    _register_workspace(workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    refs = [_Ref("opencode:ses_plain", mtime=200.0, project=str(elsewhere))]
    _stub_chat_history(monkeypatch, refs, {"opencode:ses_plain": "a plain chat"})

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert 'data-href="/chat/opencode/ses_plain"' in resp.text
    assert "ses_plain?ws=" not in resp.text


def test_search_results_workspace_column_names_the_registered_workspace(
    monkeypatch, workspace: Path, tmp_path: Path
):
    """The Workspace column sits between Provider and Status and names the
    registered workspace the hit's project maps to -- the full path rides the
    cell's tooltip. A project outside every registered workspace reads N/A,
    the same fallback the Status column uses for a plain chat."""
    _register_workspace(workspace)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    refs = [
        _Ref("opencode:ses_in_ws", mtime=300.0, project=str(workspace)),
        _Ref("opencode:ses_outside", mtime=200.0, project=str(elsewhere)),
    ]
    transcripts = {
        "opencode:ses_in_ws": "a plain chat",
        "opencode:ses_outside": "another plain chat",
    }
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    html = resp.text
    assert resp.status_code == 200
    assert (
        "<th>Prompt</th><th>Provider</th><th>Workspace</th>"
        "<th>Status</th><th>Updated</th>" in html
    )

    def _workspace_cell(conversation_id: str) -> str:
        row = next(r for r in _data_rows(html) if conversation_id in r)
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)
        assert len(cells) == 5  # prompt, provider, workspace, status, updated
        return cells[2].strip()

    resolved = str(workspace.resolve())
    assert _workspace_cell("ses_in_ws") == (
        f'<span title="{resolved}">{workspace.name}</span>'
    )
    assert _workspace_cell("ses_outside") == "N/A"


def test_search_unresolved_task_hit_fallback_stays_bare(
    monkeypatch, workspace: Path
):
    """A managed task hit whose lane does not resolve falls back to a plain
    chat row. Its project is a registered workspace, yet the href stays bare:
    the task's ws mapping is only for resolved lanes."""
    _register_workspace(workspace)
    refs = [_Ref("opencode:ses_orphan", mtime=400.0, project=str(workspace))]
    transcripts = {"opencode:ses_orphan": f"{_MARKER}\n## USER\norphan body\n"}
    _stub_chat_history(monkeypatch, refs, transcripts)

    with _client() as client:
        resp = client.get("/search/results", params={"q": ""})

    assert 'data-href="/chat/opencode/ses_orphan"' in resp.text
    assert "ses_orphan?ws=" not in resp.text


class _ShellSessionStub:
    """Knows one shell session, so the shell page can render without a child."""

    SESSION_ID = "abc123def456"

    def get_session(self, session_id: str):
        return SimpleNamespace(cmd=["opencode"]) if session_id == self.SESSION_ID else None


@pytest.mark.parametrize(
    "provider, conversation_id",
    [("opencode", "ses_plain"), ("cline", "sess_plain")],  # Resume and Fork-only providers
)
def test_search_row_to_resume_back_url_to_shell_shows_generate_task(
    monkeypatch, workspace: Path, provider: str, conversation_id: str
):
    """The full chain of requirement 2: a search row links to the transcript
    with ``?ws=``; the transcript's Resume/Fork forms carry that ``back_url``;
    the shell opened from it shows Generate Task for the same workspace."""
    ws_id = _register_workspace(workspace)
    key = f"{provider}:{conversation_id}"
    refs = [_Ref(key, mtime=200.0, project=str(workspace))]
    _stub_chat_history(monkeypatch, refs, {key: "a plain chat"})
    monkeypatch.setattr(shell_routes, "get_manager", lambda: _ShellSessionStub())

    with _client() as client:
        search = client.get("/search/results", params={"q": ""}).text
        href = re.search(rf'data-href="(/chat/{provider}/{conversation_id}\?ws=\d+)"', search)
        assert href is not None, search
        chat = client.get(href.group(1)).text
        assert 'id="generate-task-form"' in chat
        assert f'action="/chat/{provider}/{conversation_id}/generate-task"' in chat
        assert f'<input type="hidden" name="ws" value="{ws_id}">' in chat
        shell = client.get(
            f"/shell/{_ShellSessionStub.SESSION_ID}",
            params={
                "from": _hidden_input(chat, "back_url"),
                "label": _hidden_input(chat, "back_label"),
            },
        ).text

    assert f'action="/chat/{provider}/{conversation_id}/generate-task"' in shell
    assert f'<input type="hidden" name="ws" value="{ws_id}">' in shell


def test_chat_fork_form_recarries_workspace_in_back_url(monkeypatch, workspace: Path):
    """The fork form shares the chat page's back-nav payload, so it must
    re-carry ``?ws=`` too (the redirect reaches the shell the same way)."""
    from backseat_harness import history

    ws_id = _register_workspace(workspace)
    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {k: "ide chat body" for k in keys},
    )

    with _client() as client:
        resp = client.get(f"/chat/cline/sess_fork?ws={ws_id}")

    assert _hidden_input(resp.text, "back_url") == f"/chat/cline/sess_fork?ws={ws_id}"
    assert _hidden_input(resp.text, "back_label") == workspace.name


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



# --- shell heading: title line over the session id ----------------------------


def _shell_heading(html: str) -> str:
    """The header's top line: the ``<strong>`` text inside the shell header."""
    header = html[html.index('<header class="chat-header">') : html.index("</header>")]
    return re.search(r"<strong>(.*?)</strong>", header, re.S).group(1)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # Task page / task chat: the task name.
        ({"title": "fix-login-flow"}, "fix-login-flow"),
        # Plain chat: the prompt excerpt the chat page already shows.
        ({"title": "fix the flaky test now"}, "fix the flaky test now"),
        # Workspace "+ New chat": the workspace and provider.
        ({"title": "New my-ws kiro-cli"}, "New my-ws kiro-cli"),
        # No title known: the command is the fallback.
        ({}, None),
    ],
)
def test_shell_heading_is_title_then_session_id(tmp_path: Path, params, expected):
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(f"/shell/{session_id}", params=params)
            assert page.status_code == 200
            heading = _shell_heading(page.text)
            assert heading == (expected if expected is not None else " ".join(cmd))
            # The session id stays on its own line under the heading.
            assert f"<small>{session_id}</small>" in page.text
        finally:
            _kill_session(session_id)


def test_shell_heading_escapes_and_bounds_user_title(tmp_path: Path):
    """Titles are user text: markup is escaped, and a hand-written overlong
    ``?title=`` is cut to the 160-character bound with an ellipsis."""
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    hostile = "<img src=x onerror=alert(1)>  " + "a" * 400
    with _client() as client:
        session_id = _start_session(client, cmd, tmp_path)
        try:
            page = client.get(f"/shell/{session_id}", params={"title": hostile})
            assert page.status_code == 200
            heading = _shell_heading(page.text)
            assert "<img" not in heading
            assert heading.startswith("&lt;img src=x onerror=alert(1)&gt; a")
            assert heading.endswith("...")
            assert len(unescape(heading)) == 160
        finally:
            _kill_session(session_id)


def test_task_takeover_title_is_the_task_slug(workspace: Path):
    ws_id = _register_workspace(workspace)
    lane = workspace / ".backseat" / "tasks" / "title-lane"
    lane.mkdir(parents=True)
    (lane / "goal.md").write_text("goal\n", encoding="utf-8")
    (lane / "state.json").write_text(
        json.dumps(
            {
                "slug": "title-lane",
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
        page = client.get(f"/ws/{ws_id}/tasks/title-lane")

    assert _hidden_input(page.text, "title") == "title-lane"


def test_chat_fork_title_is_the_prompt_excerpt(monkeypatch):
    from backseat_harness import history

    monkeypatch.setattr(
        history,
        "load_sessions",
        lambda keys, providers=None: {
            k: "## USER\nfix the\tflaky test now\n## ASSISTANT\ndone" for k in keys
        },
    )

    with _client() as client:
        resp = client.get("/chat/cline/sess_title")

    assert _hidden_input(resp.text, "title") == "fix the flaky test now"


def test_search_fork_redirect_carries_title(monkeypatch, tmp_path):
    refs = [_Ref("kiro_ide:sess_cur", mtime=200.0, project=None)]
    _stub_chat_history(monkeypatch, refs, {"kiro_ide:sess_cur": "kiro ide chat"})
    _capture_fork_command(monkeypatch, tmp_path)

    with _client() as client:
        resp = client.post(
            "/search/fork",
            data={
                "conversation_id": "sess_cur",
                "current_session_key": "kiro_ide:sess_cur",
                "back_url": "/chat/kiro_ide/sess_cur",
                "back_label": "Workspaces",
                "title": "fix the flaky test now",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/shell/abc123def456?from=%2Fchat%2Fkiro_ide%2Fsess_cur"
        "&label=Workspaces&title=fix+the+flaky+test+now"
    )

"""Generate Task on the shell page: shown only for a chat reached with ``?ws=N``.

The shell page is served from a fake session (no real child), so these tests
check what the page renders for each back URL the shell can receive.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backseat_harness import db, history
from backseat_harness.web.main import create_app
from backseat_harness.web.routes import shell as shell_routes

PORT = 8080
SESSION_ID = "abc123def456"
TRANSCRIPT = "User: keep it to the token budget guard\nAssistant: noted"
FORM = 'id="generate-task-form"'


class _FakeManager:
    """Knows one session: enough for ``shell_page`` to render."""

    def get_session(self, session_id: str):
        return SimpleNamespace(cmd=["bash"]) if session_id == SESSION_ID else None


@pytest.fixture
def fake_shell(monkeypatch):
    monkeypatch.setattr(shell_routes, "get_manager", lambda: _FakeManager())


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


def _shell(client: TestClient, **kwargs):
    return client.get(f"/shell/{SESSION_ID}", **kwargs)


def _from(url: str) -> dict:
    return {"params": {"from": url}}


# --- visibility matrix ---------------------------------------------------------


@pytest.mark.parametrize("source", ["from", "referer", "written-just-now"])
def test_form_shown_for_a_known_chat_with_a_registered_ws(
    workspace, chat, fake_shell, monkeypatch, source
):
    ws_id = _register(workspace)
    url = f"/chat/opencode/ses_x?ws={ws_id}"
    if source == "written-just-now":
        # No liveness rule: a chat written a moment ago still gets the form.
        monkeypatch.setattr(
            history, "load_sessions", lambda keys, providers=None: {k: TRANSCRIPT for k in keys}
        )
    with _client() as client:
        if source == "referer":
            html = _shell(client, headers={"referer": f"http://localhost:{PORT}{url}"}).text
        else:
            html = _shell(client, **_from(url)).text

    assert FORM in html
    assert 'action="/chat/opencode/ses_x/generate-task"' in html
    assert f'<input type="hidden" name="ws" value="{ws_id}">' in html
    if source == "from":
        message = json.loads(re.search(r"const message = (.+);", html).group(1))
        assert f"Target workspace: {workspace.name}\nPath: {workspace.resolve()}" in message


def test_the_shell_back_link_is_the_chat_it_came_from(workspace, chat, fake_shell):
    ws_id = _register(workspace)
    with _client() as client:
        html = _shell(client, **_from(f"/chat/opencode/ses_x?ws={ws_id}")).text

    assert f'<a class="chat-header-back" href="/chat/opencode/ses_x?ws={ws_id}">' in html


@pytest.mark.parametrize(
    "back, referer",
    [
        ("/chat/opencode/ses_x", None),  # no ws
        ("/chat/opencode/ses_x?ws=999", None),  # unregistered
        ("/chat/opencode/ses_x?ws=abc", None),  # not an integer
        ("/chat/opencode/ses_x?ws=0", None),  # below the id range
        ("/chat/opencode/ses_x?ws=-1", None),
        ("/chat/opencode/ses_x?ws=", None),  # blank
        ("/chat/opencode/ses_x?ws=1&ws=2", None),  # ambiguous
        ("/chat/not_a_provider/ses_x?ws=1", None),  # unknown provider
        ("/chat/opencode/ses_x/extra?ws=1", None),  # extra path segment
        ("/ws/1", None),  # the workspace New chat page
        ("/ws/1/tasks/lane", None),  # a task page
        ("/", None),  # the homepage
        ("/search/results?q=x", None),  # any other page
        (None, "http://evil.com/chat/opencode/ses_x?ws=1"),  # foreign referer
    ],
)
def test_no_form_for_a_back_url_that_is_not_a_chat_with_a_registered_ws(
    workspace, chat, fake_shell, back, referer
):
    ws_id = _register(workspace)
    kwargs = _from(back.replace("ws=1", f"ws={ws_id}")) if back else {}
    headers = {"referer": referer.replace("ws=1", f"ws={ws_id}")} if referer else None
    with _client() as client:
        resp = _shell(client, headers=headers, **kwargs)

    assert resp.status_code == 200
    assert FORM not in resp.text
    assert 'name="ws"' not in resp.text


def test_bare_shell_with_no_back_url_shows_no_form(workspace, chat, fake_shell):
    _register(workspace)
    with _client() as client:
        html = _shell(client).text

    assert FORM not in html


def test_transcript_back_url_round_trips_into_the_shell(workspace, chat, fake_shell):
    """The chat page's hidden back_url, passed as the shell's ``from``, shows the form."""
    ws_id = _register(workspace)
    with _client() as client:
        chat_html = client.get(f"/chat/opencode/ses%3Fa?ws={ws_id}").text
        assert f'value="/chat/opencode/ses%3Fa?ws={ws_id}"' in chat_html
        html = _shell(client, **_from(f"/chat/opencode/ses%3Fa?ws={ws_id}")).text

    assert 'action="/chat/opencode/ses%3Fa/generate-task"' in html
    assert f'<input type="hidden" name="ws" value="{ws_id}">' in html


# --- parse_chat_back -----------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("/chat/opencode/ses_x?ws=3", ("opencode", "ses_x", 3)),
        ("/chat/opencode/ses%3Fa?ws=3", ("opencode", "ses?a", 3)),
        ("/chat/claude_code/a%2Fb", None),  # a slash in the id is refused
        ("/chat/opencode/ses_x?ws=3&other=1", ("opencode", "ses_x", 3)),
        ("/chat/opencode/ses_x?ws=3#frag", None),
        ("/chat/opencode/ses_x?ws=+3", None),
        ("/chat/opencode/ses_x?ws=3.0", None),
        ("/chat/opencode/ses_x?ws=٣", None),  # non-ASCII digit
        ("//chat/opencode/ses_x?ws=3", None),
        ("/\\chat/opencode/ses_x?ws=3", None),
        ("http://localhost:8080/chat/opencode/ses_x?ws=3", None),
        ("/chat/opencode/?ws=3", None),
        ("/chat/opencode?ws=3", None),
        ("/chat/OpenCode/ses_x?ws=3", None),
    ],
)
def test_parse_chat_back(url, expected):
    assert shell_routes.parse_chat_back(url) == expected

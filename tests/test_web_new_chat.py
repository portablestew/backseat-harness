"""Provider->binary map and PATH detection shared by the fork picker and the
workspace "New chat" buttons (new-chat-button plan, Step 1-2).

Detection is monkeypatched on ``shutil.which`` so nothing needs to be installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backseat_harness import db
from backseat_harness.providers import (
    available_providers,
    binary_for,
    get_provider,
    on_path_providers,
)
from backseat_harness.providers.base import ProviderError
from backseat_harness.web.main import create_app
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.shell.manager import PTYSessionManager

_PORT = 8080


def _which_only(*present: str):
    """``shutil.which`` stand-in that resolves only the named executables."""
    return lambda exe: f"/usr/bin/{exe}" if exe in present else None


def _client() -> TestClient:
    return TestClient(create_app(port=_PORT), base_url=f"http://localhost:{_PORT}")


def _register_workspace(workspace: Path) -> int:
    with db.open_db() as conn:
        assert db.add_workspace(conn, workspace) is True
        ws_id = db._workspace_id(conn, workspace)
    assert ws_id is not None
    return ws_id


@pytest.fixture
def spawned(monkeypatch) -> list[dict]:
    """Record every PTY spawn the route asks for; spawn nothing."""
    calls: list[dict] = []

    def _fake_create_session(self, cmd, cwd, cols=None, rows=None):
        calls.append({"cmd": list(cmd), "cwd": cwd})
        return "abc123def456"

    monkeypatch.setattr(PTYSessionManager, "create_session", _fake_create_session)
    return calls


def test_binary_for_maps_every_registered_provider():
    assert binary_for("claude") == "claude"
    assert binary_for("kiro") == "kiro-cli"
    assert binary_for("opencode") == "opencode"


def test_binary_for_unknown_provider_raises():
    with pytest.raises(ProviderError, match="no CLI binary known"):
        binary_for("cursor")


def test_on_path_providers_keeps_registry_order_and_skips_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", _which_only("opencode", "claude", "kiro-cli"))
    assert on_path_providers() == ["claude", "kiro", "opencode"]

    monkeypatch.setattr("shutil.which", _which_only("claude", "opencode"))
    assert on_path_providers() == ["claude", "opencode"]


def test_on_path_providers_empty_when_nothing_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", _which_only())
    assert on_path_providers() == []


@pytest.mark.parametrize("name", available_providers())
def test_binary_matches_adapter_interactive_argv0(name):
    # Drift guard: the shared map must name the executable each adapter
    # actually launches for a fresh chat (no native session id).
    assert binary_for(name) == get_provider(name).interactive_command(None)[0]


def test_pick_fork_provider_scan_order_is_opencode_first(monkeypatch):
    # Behaviour preserved through the shared helper: when several CLIs are
    # present, the fork picker still prefers opencode, then kiro, then claude.
    monkeypatch.setattr("shutil.which", _which_only("claude", "opencode"))
    assert chat_search_service.pick_fork_provider("unknown:sess_cur", []) == "opencode"


# --- Step 3: POST /ws/{id}/chats -----------------------------------------------


@pytest.mark.parametrize(
    ("provider", "argv"),
    [
        ("opencode", ["/usr/bin/opencode"]),
        ("kiro", ["/usr/bin/kiro-cli", "chat"]),
        ("claude", ["/usr/bin/claude"]),
    ],
)
def test_new_chat_spawns_fresh_provider_argv_and_redirects(
    monkeypatch, workspace: Path, spawned, provider, argv
):
    ws_id = _register_workspace(workspace)
    monkeypatch.setattr("shutil.which", _which_only("opencode", "kiro-cli", "claude"))

    with _client() as client:
        resp = client.post(
            f"/ws/{ws_id}/chats", data={"provider": provider}, follow_redirects=False
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == (
        f"/shell/abc123def456?from=%2Fws%2F{ws_id}&label={workspace.name}"
    )
    assert len(spawned) == 1
    assert spawned[0]["cmd"] == argv
    assert Path(spawned[0]["cwd"]).resolve() == workspace.resolve()


def test_new_chat_absent_provider_is_400_and_spawns_nothing(
    monkeypatch, workspace: Path, spawned
):
    ws_id = _register_workspace(workspace)
    # kiro is registered but not installed on this host.
    monkeypatch.setattr("shutil.which", _which_only("opencode", "claude"))

    with _client() as client:
        resp = client.post(
            f"/ws/{ws_id}/chats", data={"provider": "kiro"}, follow_redirects=False
        )

    assert resp.status_code == 400
    assert "kiro is not on PATH" in resp.text
    assert spawned == []


def test_new_chat_unknown_provider_is_400_and_spawns_nothing(
    monkeypatch, workspace: Path, spawned
):
    ws_id = _register_workspace(workspace)
    monkeypatch.setattr("shutil.which", _which_only("opencode", "kiro-cli", "claude"))

    with _client() as client:
        resp = client.post(
            f"/ws/{ws_id}/chats", data={"provider": "cursor"}, follow_redirects=False
        )

    assert resp.status_code == 400
    assert "unknown provider" in resp.text
    assert spawned == []


def test_new_chat_unknown_workspace_is_404_and_spawns_nothing(
    monkeypatch, spawned
):
    monkeypatch.setattr("shutil.which", _which_only("claude"))

    with _client() as client:
        resp = client.post(
            "/ws/999999/chats", data={"provider": "claude"}, follow_redirects=False
        )

    assert resp.status_code == 404
    assert spawned == []


# --- Step 4: workspace "+ New chat" expander -----------------------------------


def _new_chat_panel(page: str) -> str:
    start = page.index('id="new-chat"')
    return page[start : page.index("</details>", start)]


def test_new_chat_expander_sits_after_other_chats(monkeypatch, workspace: Path):
    ws_id = _register_workspace(workspace)
    monkeypatch.setattr("shutil.which", _which_only("claude"))

    with _client() as client:
        page = client.get(f"/ws/{ws_id}").text

    assert 'class="expander new-chat"' in page
    panel = _new_chat_panel(page)
    assert 'class="expander-plus"' in panel
    assert ">+</summary>" not in panel
    assert "<strong>New chat</strong>" in panel
    # Placed after the Other chats table, i.e. after its closing </table>.
    other = page.index("<h4>Other chats</h4>")
    assert page.index('id="new-chat"') > page.index("</table>", other)


def test_new_chat_expander_renders_form_per_on_path_provider(
    monkeypatch, workspace: Path
):
    ws_id = _register_workspace(workspace)
    monkeypatch.setattr("shutil.which", _which_only("opencode", "kiro-cli"))

    with _client() as client:
        panel = _new_chat_panel(client.get(f"/ws/{ws_id}").text)

    assert panel.count(f'action="/ws/{ws_id}/chats"') == 2
    assert 'name="provider" value="kiro"' in panel
    assert 'name="provider" value="opencode"' in panel
    # The kiro button shows the CLI's display name, not the registry key.
    assert ">kiro-cli</button>" in panel
    assert 'value="claude"' not in panel
    # Stacked in registry order: kiro-cli above opencode.
    assert panel.index("kiro-cli") < panel.index(">opencode</button>")
    assert "No supported CLI found on PATH." not in panel


def test_new_chat_expander_none_on_path_shows_note_and_no_form(
    monkeypatch, workspace: Path
):
    ws_id = _register_workspace(workspace)
    monkeypatch.setattr("shutil.which", _which_only())

    with _client() as client:
        resp = client.get(f"/ws/{ws_id}")

    assert resp.status_code == 200
    panel = _new_chat_panel(resp.text)
    assert 'class="expander-plus"' in panel
    assert "No supported CLI found on PATH." in panel
    assert f'action="/ws/{ws_id}/chats"' not in panel
    assert 'name="provider"' not in panel

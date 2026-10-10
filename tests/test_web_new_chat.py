"""Provider->binary map and PATH detection shared by the fork picker and the
workspace "New chat" buttons (new-chat-button plan, Step 1-2).

Detection is monkeypatched on ``shutil.which`` so nothing needs to be installed.
"""

from __future__ import annotations

import pytest

from backseat_harness.providers import (
    available_providers,
    binary_for,
    get_provider,
    on_path_providers,
)
from backseat_harness.providers.base import ProviderError
from backseat_harness.web.services import chat_search as chat_search_service


def _which_only(*present: str):
    """``shutil.which`` stand-in that resolves only the named executables."""
    return lambda exe: f"/usr/bin/{exe}" if exe in present else None


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

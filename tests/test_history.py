"""Tests for the chat-mother-forker seam.

These run against the real library (it is a core dependency) but not against any
particular stored session, so they stay deterministic on any machine. The
happy-path lookups are covered in the loop tests via the ``stub_history``
fixture.
"""

import pytest

from backseat_harness import history
from backseat_harness.providers.opencode import OpencodeProvider


def test_no_match_renders_as_empty_string():
    """The library returns a "No conversation found" sentinel, not an error.

    The loop treats "no history yet" and "empty history" the same, so that
    sentinel must never reach a prompt as if it were transcript content.
    """
    assert history.fork_session("ses_definitely-not-a-real-session-xyz", "opencode") == ""


def test_missing_session_is_none_not_an_error():
    assert history.find_session("ses_definitely-not-a-real-session-xyz", "opencode") is None


def test_unknown_history_provider_fails_loudly():
    # A typo'd history_provider would otherwise silently disable compaction.
    with pytest.raises(history.HistoryError, match="no provider named"):
        history.find_session("anything", "not_a_real_provider")


def test_opencode_history_provider_name_is_valid():
    # Guards the one string linking an adapter to its history backend.
    assert history.find_session("ses_nope-xyz", OpencodeProvider.history_provider) is None


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("opencode:ses_abc", "ses_abc"),
        ("kiro_ide_v2:sess_1-2-3", "sess_1-2-3"),
        ("bare-id-no-prefix", "bare-id-no-prefix"),
    ],
)
def test_id_from_key(key, expected):
    assert history._id_from_key(key) == expected


def test_load_full_transcript_no_match_is_empty():
    """load_chat raises LookupError on no match; the seam maps that to "".

    The loop treats "no session yet" and "empty session" the same, so the
    LookupError must never surface as a crash mid-capture.
    """
    assert history.load_full_transcript("ses_not-a-real-session-xyz", "opencode") == ""


def test_load_full_transcript_returns_load_chat_string(monkeypatch):
    """The seam passes load_chat's rendered transcript straight through."""
    monkeypatch.setattr(history, "_scoped", lambda provider: ["provider"])
    monkeypatch.setattr(
        history, "_load", lambda attr: (lambda search, providers=None: "## USER\nrendered")
    )
    assert history.load_full_transcript("root", "opencode") == "## USER\nrendered"

"""Tests for the chat-mother-forker seam.

These run against the real library (it is a core dependency) and against the
machine's real stored sessions, so the no-match sentinels are generated per
run: chat-mother-forker's ``find_chats`` content-searches stored transcripts,
and a fixed literal gets quoted into them by the very sessions that read or
fail this file, then matches forever. The happy-path lookups are covered in
the loop tests via the ``stub_history`` fixture.
"""

import uuid

import pytest

from backseat_harness import history
from backseat_harness.providers.opencode import OpencodeProvider

# A fresh uuid can never appear in any stored transcript, so it can't
# self-poison the content search no matter how often this file is read.
_NO_SUCH_SESSION = f"ses_{uuid.uuid4().hex}"


def test_no_match_renders_as_empty_string():
    """The library returns a "No conversation found" sentinel, not an error.

    The loop treats "no history yet" and "empty history" the same, so that
    sentinel must never reach a prompt as if it were transcript content.
    """
    assert history.fork_session(_NO_SUCH_SESSION, "opencode") == ""


def test_missing_session_is_none_not_an_error():
    assert history.find_session(_NO_SUCH_SESSION, "opencode") is None


def test_unknown_history_provider_fails_loudly():
    # A typo'd history_provider would otherwise silently disable compaction.
    with pytest.raises(history.HistoryError, match="no provider named"):
        history.find_session("anything", "not_a_real_provider")


def test_opencode_history_provider_name_is_valid():
    # Guards the one string linking an adapter to its history backend.
    assert history.find_session(_NO_SUCH_SESSION, OpencodeProvider.history_provider) is None


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
    assert history.load_full_transcript(_NO_SUCH_SESSION, "opencode") == ""


def test_load_full_transcript_returns_load_chat_string(monkeypatch):
    """The seam passes load_chat's rendered transcript straight through."""
    monkeypatch.setattr(history, "_scoped", lambda provider: ["provider"])
    monkeypatch.setattr(
        history, "_load", lambda attr: (lambda search, providers=None: "## USER\nrendered")
    )
    assert history.load_full_transcript("root", "opencode") == "## USER\nrendered"


# --- Contention: retry, then degrade (never crash a running lane) ---


def _stub_lib(monkeypatch, fn):
    """Point the whole seam at a fake chat-mother-forker entry point."""
    monkeypatch.setattr(history, "_scoped", lambda provider: ["provider"])
    monkeypatch.setattr(history, "_load", lambda attr: fn)


def _no_sleep(monkeypatch) -> list:
    """Record requested delays instead of actually waiting."""
    slept: list = []
    monkeypatch.setattr(history.time, "sleep", slept.append)
    return slept


class _Ref:
    """Minimal stand-in for chat-mother-forker's ChatRef."""

    def __init__(self):
        self.key = "opencode:ses_1"
        self.session = "ses_1"
        self.size_bytes = 4096
        self.mtime = 1.0
        self.project = None


def test_contended_read_is_retried_and_still_answers(monkeypatch, capsys):
    """Two transient failures then success: the caller never learns there was one.

    This is the real shape of the failure -- a read-only opencode connection
    racing the live CLI's writer over a WAL database, which surfaces as
    ``sqlite3.OperationalError: disk I/O error``.
    """
    calls = []

    def flaky(search, providers=None, limit=None):
        calls.append(limit)
        if len(calls) < 3:
            raise OSError("disk I/O error")
        return [_Ref()]

    _stub_lib(monkeypatch, flaky)
    slept = _no_sleep(monkeypatch)

    info = history.find_session("root", "opencode")

    assert info.native_id == "ses_1"
    assert info.size_bytes == 4096
    assert len(calls) == 3
    # One second between attempts, and only between them.
    assert slept == [history.RETRY_DELAY_S, history.RETRY_DELAY_S]
    assert capsys.readouterr().err.count("retrying") == 2


def test_gives_up_after_the_attempt_budget_and_falls_back(monkeypatch, capsys):
    """A store that stays unreadable degrades to 'no session', loudly."""
    def always_fails(search, providers=None, limit=None):
        raise OSError("disk I/O error")

    _stub_lib(monkeypatch, always_fails)
    slept = _no_sleep(monkeypatch)

    assert history.find_session("root", "opencode") is None
    assert len(slept) == history.RETRY_ATTEMPTS - 1  # no sleep after the last try
    err = capsys.readouterr().err
    assert "after 3 attempts" in err
    assert "disk I/O error" in err


def test_fork_and_transcript_degrade_to_empty(monkeypatch, capsys):
    """The two rendering reads fall back to "", each already documented for it.

    An empty fork means the compaction preamble is skipped and the operator
    reviews without its transcript -- a weak turn, not a dead lane.
    """
    def always_fails(search, providers=None, limit=None):
        raise OSError("disk I/O error")

    _stub_lib(monkeypatch, always_fails)
    _no_sleep(monkeypatch)

    assert history.fork_session("root", "opencode") == ""
    assert history.load_full_transcript("root", "opencode") == ""
    assert "continuing without it" in capsys.readouterr().err


def test_history_error_is_raised_not_retried(monkeypatch, capsys):
    """A misconfigured provider is a config bug, not contention: fail at once.

    Retrying it would spend two wasted seconds and then hand the caller a
    fallback that looks like "no session yet" -- a lane that can never compact,
    with the real cause buried under three warnings.
    """
    calls = []

    def misconfigured(search, providers=None, limit=None):
        calls.append(limit)
        raise history.HistoryError("chat-mother-forker has no provider named 'nope'")

    _stub_lib(monkeypatch, misconfigured)
    slept = _no_sleep(monkeypatch)

    with pytest.raises(history.HistoryError, match="no provider named"):
        history.find_session("root", "opencode")
    assert len(calls) == 1
    assert slept == []

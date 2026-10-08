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
from chat_mother_forker.models import Conversation, ConversationRef, Message, Role

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


# --- Batched exact-key reads: one listing, one parse per key (load_sessions) ---
#
# Unlike the tests above these stub the seam, because what they pin is the
# *shape* of the lookup -- what gets listed and what gets parsed -- rather than
# a result read off this machine. The shape is the fix: a per-row tiered search
# re-parses every stored conversation for every row (147s for one workspace
# page; the regression guard is test_web's batch test).


class _FakeProvider:
    """Stand-in for a chat-mother-forker provider: cheap list, one parse per load."""

    name = "fake"

    def __init__(self, conversations: dict[str, str]):
        self._conversations = conversations
        self.listed = 0
        self.loaded: list[str] = []

    def list_candidates(self):
        self.listed += 1
        for conversation_id in self._conversations:
            yield ConversationRef(
                provider=self.name,
                conversation_id=conversation_id,
                locator=conversation_id,
                mtime=1.0,
            )

    def load(self, ref: ConversationRef) -> Conversation:
        self.loaded.append(ref.conversation_id)
        return Conversation(
            ref=ref,
            messages=[
                Message(role=Role.USER, text=self._conversations[ref.conversation_id])
            ],
        )

    def conversation_metadata(self, ref: ConversationRef):
        # File-backed providers return (None, None); same here.
        return None, None


def _stub_providers(monkeypatch, providers: list[_FakeProvider]) -> None:
    def _fake_load(attr: str):
        assert attr == "default_providers", f"unexpected history seam: {attr}"
        return lambda: list(providers)

    monkeypatch.setattr(history, "_load", _fake_load)


def test_load_sessions_lists_once_and_parses_only_the_requested_keys(monkeypatch):
    provider = _FakeProvider({"ses_a": "alpha text", "ses_b": "beta text"})
    _stub_providers(monkeypatch, [provider])

    rendered = history.load_sessions(["fake:ses_a", "fake:ses_b"])

    # Both transcripts come back, rendered the way load_chat renders them.
    assert rendered["fake:ses_a"] == "## USER\nUSER\n> alpha text"
    assert rendered["fake:ses_b"] == "## USER\nUSER\n> beta text"
    # Two keys, one provider -> listed once, and only the two asked-for
    # conversations parsed (the rest of a real store is never touched).
    assert provider.listed == 1
    assert provider.loaded == ["ses_a", "ses_b"]


def test_load_sessions_drops_unknown_keys_without_parsing_anything(monkeypatch):
    provider = _FakeProvider({"ses_a": "alpha text"})
    _stub_providers(monkeypatch, [provider])

    # Unknown session, unknown provider, malformed key: all absent and none
    # fatal. The known provider is listed (that is how the session is looked
    # up) but nothing is parsed; the unknown provider is skipped outright.
    rendered = history.load_sessions(["fake:ses_gone", "ghost:ses_a", "no-colon"])

    assert rendered == {}
    assert provider.listed == 1
    assert provider.loaded == []


def test_memoized_providers_share_one_parse_across_passes(monkeypatch):
    """The point of the memo: a search and its transcript fetch parse once."""
    provider = _FakeProvider({"ses_a": "alpha text", "ses_b": "beta text"})
    _stub_providers(monkeypatch, [provider])

    providers = history.memoized_providers()
    assert [p.name for p in providers] == ["fake"]

    candidates = list(providers[0].list_candidates())
    # Pass one (the search) parses both rows; pass two (the transcript fetch)
    # asks again and gets them from the block's map, not the store.
    providers[0].load(candidates[0])
    providers[0].load(candidates[1])
    providers[0].load(candidates[0])
    assert provider.loaded == ["ses_a", "ses_b"]

    # Listing is cheap and deliberately not memoized.
    assert provider.listed == 1

    # The map dies with the returned list: a later request reads the store
    # afresh rather than showing a stale transcript.
    history.memoized_providers()[0].load(candidates[0])
    assert provider.loaded == ["ses_a", "ses_b", "ses_a"]


def test_list_session_refs_parses_nothing_until_project_is_read(monkeypatch):
    provider = _FakeProvider({"ses_a": "alpha text"})
    _stub_providers(monkeypatch, [provider])

    refs = history.list_session_refs(limit=5)

    # One cheap scan, zero parses, and every field a keys-only caller reads.
    assert [r.key for r in refs] == ["fake:ses_a"]
    assert refs[0].native_id == "ses_a"
    assert refs[0].mtime == 1.0
    assert provider.listed == 1
    assert provider.loaded == []

    # project lives on the parsed conversation: read it once and it parses
    # this one session; read it again and the cache answers.
    assert refs[0].project is None
    assert provider.loaded == ["ses_a"]
    _ = refs[0].project
    assert provider.loaded == ["ses_a"]

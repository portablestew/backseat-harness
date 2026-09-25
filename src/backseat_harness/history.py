"""Managed-session history access, via chat-mother-forker.

Everything the harness knows about a managed session's *stored* form comes
through this module: its byte size (the compaction trigger) and a rendered fork
of it (the compaction preamble and the system agent's review context).

Centralizing it here is what lets the harness stay ignorant of provider storage
internals. opencode keeps transcripts in SQLite; Claude Code keeps JSONL files.
chat-mother-forker's ``ChatRef.size_bytes`` normalizes both to a byte count, so
the harness never stats a transcript file itself -- which matters because the
v0.1 provider (opencode) has no transcript file to stat.

Lookups are scoped to a single chat-mother-forker provider (the managed CLI's
own storage) so a search for a task's marker can't match some other tool's
session, and so the scan stays cheap.

chat-mother-forker is imported lazily: ``init`` / ``status`` / ``list`` stay
usable in an environment where it is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass

# chat-mother-forker returns this (rather than raising) when nothing matched.
_NO_MATCH_PREFIX = "No conversation found"


class HistoryError(RuntimeError):
    """Raised when chat history is unavailable or a provider name is unknown."""


@dataclass(frozen=True)
class SessionInfo:
    """A stored managed session, as much as the harness needs to know about it."""

    #: "provider:conversation_id" -- the exact string fork_session accepts.
    key: str
    #: The CLI's own session id (what you pass to `opencode run --session`).
    native_id: str
    #: Raw stored size. Units differ per provider (file bytes vs. summed row
    #: bytes), so compare against a threshold, never across providers.
    size_bytes: int
    #: Last-modified, epoch seconds.
    mtime: float
    #: Best-effort workspace directory name, or None.
    project: str | None = None


def find_session(search: str, history_provider: str) -> SessionInfo | None:
    """Return the newest stored session matching ``search``, or None.

    ``search`` is normally a task's root UUID (the ``<meta>`` marker), which
    matches every generation of that task's chain; the newest one wins.

    The returned ``size_bytes`` is a snapshot taken now, so call this right at
    the decision point rather than caching it across a managed turn.
    """
    find_chats = _load("find_chats")
    rows = find_chats(search, providers=_scoped(history_provider), limit=1)
    if not rows:
        return None
    ref = rows[0]
    return SessionInfo(
        key=ref.key,
        native_id=ref.session or _id_from_key(ref.key),
        size_bytes=ref.size_bytes,
        mtime=ref.mtime,
        project=ref.project,
    )


def fork_session(search: str, history_provider: str) -> str:
    """Render the newest session matching ``search`` as a transcript string.

    Middle-truncated by chat-mother-forker, which is what makes it usable as a
    compaction preamble: the expensive tool payloads in the middle are dropped
    while intent (start) and conclusion (end) survive. Returns "" when nothing
    matched, so callers can treat "no history" and "empty history" alike.
    """
    fork_chat = _load("fork_chat")
    text = fork_chat(search, providers=_scoped(history_provider))
    if not text or text.startswith(_NO_MATCH_PREFIX):
        return ""
    return text


def _id_from_key(key: str) -> str:
    """Strip the "provider:" prefix off a ChatRef key."""
    _, _, conversation_id = key.partition(":")
    return conversation_id or key


def _scoped(history_provider: str) -> list:
    """Return just the one chat-mother-forker provider backing this CLI."""
    default_providers = _load("default_providers")
    provs = [p for p in default_providers() if p.name == history_provider]
    if not provs:
        raise HistoryError(
            f"chat-mother-forker has no provider named {history_provider!r}; "
            "cannot read this CLI's chat history"
        )
    return provs


def _load(attr: str):
    """Lazily fetch a name from chat-mother-forker's public API."""
    try:
        import chat_mother_forker
    except ImportError as exc:  # pragma: no cover - dependency-optional path
        raise HistoryError(
            "reading managed chat history requires chat-mother-forker>=0.2.0; "
            "install it to enable compaction and system-agent review"
        ) from exc
    try:
        return getattr(chat_mother_forker, attr)
    except AttributeError as exc:  # pragma: no cover - version mismatch
        raise HistoryError(
            f"chat-mother-forker is installed but does not export {attr!r}; "
            "backseat-harness needs >=0.2.0"
        ) from exc

"""Managed-session history access, via chat-mother-forker.

Everything the harness knows about a managed session's *stored* form comes
through this module: its byte size (the compaction trigger) and a rendered fork
of it (the compaction preamble and the operator's review context).

Centralizing it here is what lets the harness stay ignorant of provider storage
internals. opencode keeps transcripts in SQLite; Claude Code and kiro keep
JSONL files. chat-mother-forker's ``ChatRef.size_bytes`` normalizes these to a
byte count, so the harness never stats a transcript file itself -- which
matters because opencode has no transcript file to stat at all.

Lookups are scoped to a single chat-mother-forker provider (the managed CLI's
own storage) so a search for a task's marker can't match some other tool's
session, and so the scan stays cheap.

chat-mother-forker is imported lazily: ``init`` / ``status`` / ``list`` stay
usable in an environment where it is not installed.

Reads are retried and never fatal (see ``_retrying``). The store belongs to the
*live* managed CLI -- opencode keeps its sessions in SQLite and writes that
database while we read it -- so a read can fail on contention rather than on
anything wrong with the run. That belongs here, once, rather than at each of the
four call sites: this module is already the single door every caller comes
through, so one decorator covers the size lookup, both fork renderings, and
turn capture at the same cost of zero changes anywhere else.
"""

from __future__ import annotations

import functools
import sys
import time
from dataclasses import dataclass
from typing import Any

# chat-mother-forker returns this (rather than raising) when nothing matched.
_NO_MATCH_PREFIX = "No conversation found"

#: A read gets this many attempts, ``RETRY_DELAY_S`` apart. A contended read is
#: a blip, not a verdict, and is worth a couple of seconds of waiting rather than
#: a crashed run: a losing race lasts as long as one writer's statement.
RETRY_ATTEMPTS = 3
RETRY_DELAY_S = 1.0


class HistoryError(RuntimeError):
    """Raised when chat history is unavailable or a provider name is unknown."""


def _retrying(what: str, fallback: Any):
    """Give a history read ``RETRY_ATTEMPTS`` tries, then return ``fallback``.

    Every failure is reported on stderr -- a lane quietly missing its transcript
    and its compaction trigger is far harder to diagnose than a warning. The
    fallback is what each read already documents for "nothing there" (``None``
    or ``""``), so exhausting the attempts degrades a turn instead of ending
    the run: the operator reviews without its transcript, the last known size
    stands, and the next turn tries again.

    ``HistoryError`` is re-raised untouched and unretried. A provider name no
    ``[models]``-style config ever set is a config bug, not contention, and
    retrying it would just delay a lane that can never compact.
    """

    def decorate(read):
        @functools.wraps(read)
        def call(*args, **kwargs):
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    return read(*args, **kwargs)
                except HistoryError:
                    raise
                except Exception as exc:  # noqa: BLE001 - any store failure retries
                    if attempt == RETRY_ATTEMPTS:
                        print(
                            f"[warn] task: chat history {what} failed after "
                            f"{RETRY_ATTEMPTS} attempts ({exc}); continuing without it",
                            file=sys.stderr,
                        )
                        return fallback
                    print(
                        f"[warn] task: chat history {what} failed ({exc}); "
                        f"retrying in {RETRY_DELAY_S:g}s "
                        f"(attempt {attempt}/{RETRY_ATTEMPTS})",
                        file=sys.stderr,
                    )
                    time.sleep(RETRY_DELAY_S)

        return call

    return decorate


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


@_retrying("session lookup", fallback=None)
def find_session(search: str, history_provider: str) -> SessionInfo | None:
    """Return the newest stored session matching ``search``, or None.

    ``search`` is normally a task's root UUID (the ``<meta>`` marker), which
    matches every generation of that task's chain; the newest one wins.

    The returned ``size_bytes`` is a snapshot taken now, so call this right at
    the decision point rather than caching it across a managed turn.

    None covers both "no session yet" and "store unreadable, attempts
    exhausted"; the loop handles them alike, keeping the id the CLI reported.
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


@_retrying("session fork", fallback="")
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


@_retrying("full transcript", fallback="")
def load_full_transcript(search: str, history_provider: str) -> str:
    """Render the newest session matching ``search`` as a *complete* transcript.

    The untruncated twin of ``fork_session``: no turn cap and no middle drop, so
    the expensive tool-call payloads survive. That is what session capture needs
    -- the operator inspects real tool results rather than an elided fork. The
    string is still annotated (turn headers, ``TOOL_CALL``/``TOOL_RESULT``
    labels) by chat-mother-forker, so it reads as a conversation.

    Returns "" when nothing matched, so callers can treat "no session yet" and
    "empty session" alike -- mirroring ``fork_session`` and sparing them the
    ``LookupError`` the underlying ``load_chat`` raises.
    """
    load_chat = _load("load_chat")
    try:
        return load_chat(search, providers=_scoped(history_provider))
    except LookupError:
        return ""


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
            "reading managed chat history requires chat-mother-forker>=0.2.1; "
            "install it to enable compaction, session capture, and operator review"
        ) from exc
    try:
        return getattr(chat_mother_forker, attr)
    except AttributeError as exc:  # pragma: no cover - version mismatch
        raise HistoryError(
            f"chat-mother-forker is installed but does not export {attr!r}; "
            "backseat-harness needs >=0.2.1"
        ) from exc

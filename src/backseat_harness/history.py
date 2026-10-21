"""Managed-session history access, via chat-mother-forker.

Everything the harness knows about a managed session's *stored* form comes
through this module: its byte size (the compaction trigger) and a rendered fork
of it (the compaction preamble and the operator's review context).

Centralizing it here is what lets the harness stay ignorant of provider storage
internals. opencode keeps transcripts in SQLite; Claude Code, kiro and Command
Code keep JSONL files. chat-mother-forker's ``ChatRef.size_bytes`` normalizes these to a
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

``load_sessions`` is the exception: a batched, exact-key read for callers that
already hold keys from a listing (``find_chats`` on the search page,
``list_session_refs`` on the workspace page). It is never retried -- a page
listing degrades to fewer rows, not to a failed run -- so store failures are
swallowed per key rather than costing ``RETRY_ATTEMPTS`` sleeps each. The other
listing reads share that posture: ``memoized_providers`` gives a search and its
transcript fetch one parse per row between them, and ``list_session_refs``
answers from the cheap scan alone.
"""

from __future__ import annotations

import functools
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from backseat_harness import progress

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
            # Timed here rather than at the four call sites: this module is
            # already the single door every caller comes through, so one
            # wrapper covers the size lookup, both fork renderings, turn capture,
            # and the retry sleeps between attempts -- which is why a stalled run
            # can name the read it is stuck in instead of going quiet.
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    with progress.step(what):
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


def load_sessions(
    keys: Iterable[str], *, providers: Sequence[Any] | None = None
) -> dict[str, str]:
    """Render the stored sessions named by exact ``provider:id`` keys.

    A listing already hands the web pages a key per listed row --
    ``find_chats`` on the search page, ``list_session_refs`` on the workspace
    page -- and those pages want each row's transcript to classify it and
    preview it. Searching for them one at a time -- the only thing
    ``load_full_transcript`` can do, because it starts from a marker rather
    than a key -- re-parses *every* conversation of *every* provider once per
    row: 2.4s per row against this machine's 344 stored conversations, 147s
    for a 60-row page.

    This inverts the lookup instead: group the keys by provider, list each
    provider once (the cheap, unparsed scan), then parse only the named
    conversations -- one listing per provider and one parse per key.

    ``providers`` defaults to :func:`memoized_providers`. A caller whose refs
    came from ``find_chats`` passes the same :func:`memoized_providers` list
    that search used, so the parses it already did come back from that block's
    map: one parse per row for the whole request instead of one per pass.

    A key naming an unknown provider, a session that has since gone, or a
    conversation that will not parse is simply absent from the result;
    callers treat that as "no transcript", the same degradation a failed read
    already had.
    """
    try:
        from chat_mother_forker.turns import render_conversation
    except ImportError as exc:  # pragma: no cover - dependency-optional path
        raise HistoryError(
            "reading managed chat history requires chat-mother-forker>=0.2.1; "
            "install it to enable compaction, session capture, and operator review"
        ) from exc

    wanted: dict[str, list[str]] = {}
    for key in keys:
        provider_name, sep, conversation_id = key.partition(":")
        if not sep or not provider_name or not conversation_id:
            continue
        wanted.setdefault(provider_name, []).append(conversation_id)
    if not wanted:
        return {}

    provs = _load("default_providers")() if providers is None else providers
    by_name = {p.name: p for p in provs}
    rendered: dict[str, str] = {}
    for provider_name, conversation_ids in wanted.items():
        provider = by_name.get(provider_name)
        if provider is None:
            continue
        try:
            index = {c.conversation_id: c for c in provider.list_candidates()}
        except Exception:  # noqa: BLE001 - an unreadable store means no transcripts
            continue
        for conversation_id in dict.fromkeys(conversation_ids):
            try:
                ref = index[conversation_id]
                conversation = provider.load(ref)
            except Exception:  # noqa: BLE001 - one dead session must not drop the rest
                continue
            rendered[f"{provider_name}:{conversation_id}"] = render_conversation(
                conversation, truncate=False
            )
    return rendered


class _MemoProvider:
    """Provider proxy whose ``load`` results are shared for one request.

    ``memoized_providers`` wraps the default providers in these so that
    ``find_chats`` and :func:`load_sessions` see the *same* objects: a row the
    search already parsed comes back from the block's shared map instead of
    the store. Everything else -- ``name``, ``list_candidates``,
    ``conversation_size``, ``conversation_metadata`` -- forwards untouched,
    so the real providers keep owning their storage formats.
    """

    def __init__(self, provider: Any, parsed: dict[str, Any]) -> None:
        self._provider = provider
        self._parsed = parsed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    def load(self, ref: Any) -> Any:
        key = f"{ref.provider}:{ref.conversation_id}"
        if key not in self._parsed:
            self._parsed[key] = self._provider.load(ref)
        return self._parsed[key]


def memoized_providers() -> list[_MemoProvider]:
    """The default providers with ``load`` memoized for this call chain.

    The search page makes two passes over the chat stores: ``find_chats`` for
    the refs (it must parse every row it returns, because ``project`` lives on
    the conversation) and :func:`load_sessions` for those rows' transcripts.
    Handing both the same memoized list turns the second pass into memory
    reads -- that page from 44 parses to 24. The workspace page makes one
    pass: its refs come from the unparsed ``list_session_refs`` scan, so
    there is no first pass to share and it passes nothing.

    The map lives only as long as the returned list, which the request holds
    and drops: nothing outlives one request, so a later request reads the
    stores afresh and can never show a stale transcript.
    """
    parsed: dict[str, Any] = {}
    return [_MemoProvider(p, parsed) for p in _load("default_providers")()]


@dataclass
class SessionRef:
    """A stored session as the *cheap* listing sees it: no message bodies.

    Every field but ``project`` comes from one ``list_candidates`` per
    provider plus that provider's metadata probe -- neither parses message
    bodies, which is the expensive half of reading chat history. Callers that
    only need the key, the recency, or the store locator (fork-provider
    selection, the suggestion list's directory recovery) therefore parse
    nothing at all.

    ``project`` lives on the parsed conversation, so it resolves on first
    access -- one parse of this one session -- and caches. The suggestion
    list reads ``locator``/``database``/``session`` first and only touches
    ``project`` when that recovery came up empty, so the parse happens on the
    rare ref that needs it and never on the rest.
    """

    key: str
    native_id: str
    mtime: float
    locator: str | None = None
    database: str | None = None
    session: str | None = None
    _provider: Any = field(default=None, repr=False, compare=False)
    _candidate: Any = field(default=None, repr=False, compare=False)

    @cached_property
    def project(self) -> str | None:
        if self._provider is None or self._candidate is None:
            return None
        try:
            return self._provider.load(self._candidate).project
        except Exception:  # noqa: BLE001 - an unreadable session has no project
            return None


#: The per-provider window chat-mother-forker's own search caps at
#: (``search.CANDIDATES_PER_PROVIDER``, not exported from the package root):
#: a listing sees the same most-recent rows a search would.
_CANDIDATES_PER_PROVIDER = 100


def list_session_refs(limit: int | None = None) -> list[SessionRef]:
    """Every stored session, newest first, without parsing any of them.

    One ``list_candidates`` per provider -- the cheap, unparsed scan each
    provider documents -- capped per provider the way chat-mother-forker's
    search caps it, then merged by recency and cut to ``limit``. ``locator``
    and ``database``/``session`` come from the provider's metadata probe (a
    stored path or the ref's own fields, never a message body).
    """
    merged: list[SessionRef] = []
    for provider in _load("default_providers")():
        candidates = sorted(provider.list_candidates(), key=lambda r: r.mtime, reverse=True)
        for candidate in candidates[:_CANDIDATES_PER_PROVIDER]:
            database, session = provider.conversation_metadata(candidate)
            merged.append(
                SessionRef(
                    key=f"{candidate.provider}:{candidate.conversation_id}",
                    native_id=session or candidate.conversation_id,
                    mtime=candidate.mtime,
                    locator=None if database else candidate.locator,
                    database=database,
                    session=session,
                    _provider=provider,
                    _candidate=candidate,
                )
            )
    merged.sort(key=lambda r: r.mtime, reverse=True)
    return merged if limit is None else merged[:limit]


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

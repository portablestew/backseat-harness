"""Workspace chat search service (step 5.3, 6.1, 6.3).

Wraps chat-mother-forker search scoped to a workspace path, filtering out
backseat-managed sessions (those carrying <meta>START SESSION or
<meta>OPERATOR SESSION markers).
Also provides global session search and classification (step 6.1).
Also provides fork-provider selection helper (step 6.3).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backseat_harness import marker
from backseat_harness.web.services.workspace import _workspace_dir_for


@dataclass(frozen=True)
class ChatInfo:
    """One chat session for the workspace page listing."""

    key: str
    native_id: str
    project: str | None
    mtime: float
    preview: str
    prompt: str


_META_LINE_RE = re.compile(r"^\s*<meta>.*</meta>\s*$")


def _prompt_excerpt(text: str, limit: int = 120) -> str:
    """The chat's prompt string, per the owner brief: the first 120 characters
    of the user's prompt, with newlines and tabs replaced by spaces.

    1. Drop leading ``<meta>…</meta>`` marker line(s).
    2. Prefer the body of the first ``## USER`` turn (after the header up to
       the next ``## `` header or end of text); without a header, the whole
       remaining text.
    3. Skip leading empty lines and a bare ``USER`` role label, so the excerpt
       starts at the actual prompt text.
    4. Collapse runs of whitespace into single spaces.
    5. Cut to ``limit`` characters and append ``"..."`` when text was dropped.
    """
    if not text:
        return ""

    lines = text.splitlines()
    start = 0
    while start < len(lines) and _META_LINE_RE.match(lines[start]):
        start += 1
    body = "\n".join(lines[start:])

    header = re.search(r"^## USER\s*$", body, re.MULTILINE)
    if header:
        body = body[header.end() :]
        nxt = re.search(r"^## ", body, re.MULTILINE)
        if nxt:
            body = body[: nxt.start()]

    body_lines = body.splitlines()
    i = 0
    while i < len(body_lines):
        stripped = body_lines[i].strip()
        if stripped == "" or stripped == "USER":
            i += 1
            continue
        break
    body = "\n".join(body_lines[i:])

    collapsed = re.sub(r"\s+", " ", body).strip()
    if len(collapsed) > limit:
        return collapsed[:limit] + "..."
    return collapsed


@dataclass(frozen=True)
class SessionSearchResult:
    """One session from global search (step 6.1)."""

    key: str
    native_id: str
    project: str | None
    mtime: float
    preview: str
    classification: str  # "backseat task" | "operator" | "other"


def _normalize(path: str | Path) -> str:
    """Canonical form matching db._normalize (absolute, resolved)."""
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(path)


def _project_matches_workspace(project: str, workspace: Path) -> bool:
    """Check if a chat ref's project field matches the workspace.

    The project field from chat-mother-forker is often just the directory name.
    We match by:
    1. Exact normalized path match (if project is already a full path)
    2. Workspace name (last component) equals project
    3. Project resolves to workspace when resolved from workspace's parent
    """
    ws_norm = _normalize(workspace)
    ws_name = Path(workspace).name

    # Try exact match first
    try:
        proj_norm = _normalize(project)
        if proj_norm == ws_norm:
            return True
    except OSError:
        pass

    # Match by workspace name (common case: project is just the dir name)
    if project == ws_name:
        return True

    # Try resolving project from workspace parent directory
    try:
        ws_parent = Path(workspace).parent
        proj_resolved = _normalize(ws_parent / project)
        if proj_resolved == ws_norm:
            return True
    except OSError:
        pass

    return False


def _is_backseat_session(text: str) -> bool:
    """Return True if text contains a backseat marker (START SESSION or OPERATOR SESSION)."""
    if marker.parse_marker(text) is not None:
        return True
    if marker.OPERATOR_SESSION_RE.search(text):
        return True
    return False


def _ref_workspace_dir(ref: object, workspace: Path, ws_norm: str) -> str | None:
    """The workspace dir of ``ref`` when the chat belongs here, else ``None``.

    Store recovery first (``_workspace_dir_for``): it reads the provider's own
    store, never a message body, so the whole per-provider candidate window
    costs one directory probe per row. ``project`` -- which for a
    ``history.SessionRef`` parses that one conversation -- is consulted only
    when recovery came up empty, the posture ``_candidate_dirs`` already uses
    for the suggestion list.
    """
    raw = _workspace_dir_for(ref)
    if raw is not None:
        return raw if _normalize(raw) == ws_norm else None
    project = getattr(ref, "project", None)
    if project and _project_matches_workspace(project, workspace):
        return project
    return None


def list_recent_chats(workspace: Path | str, limit: int = 20) -> list[ChatInfo]:
    """Recent chat sessions in this workspace, excluding backseat sessions.

    The candidate window is the cheap, unparsed listing
    (``history.list_session_refs``), which takes each provider's newest
    ``_CANDIDATES_PER_PROVIDER`` rows *before* merging them -- so a chatty
    provider cannot push another provider's chats out of the window the way a
    single global recency cut did. Rows are then narrowed to this workspace by
    store recovery (no message bodies), their transcripts are read in
    newest-first batches until ``limit`` survivors remain (never one row at a
    time), backseat-marked and empty sessions are dropped, and the newest
    ``limit`` rows come back newest first (by mtime).
    """
    ws_path = Path(workspace)

    try:
        from backseat_harness import history
    except ImportError:
        return []

    try:
        refs = history.list_session_refs()
    except Exception:
        return []

    ws_norm = _normalize(ws_path)
    ws_candidates = []
    for ref in refs:
        project_dir = _ref_workspace_dir(ref, ws_path, ws_norm)
        if project_dir is not None:
            ws_candidates.append((ref, project_dir))

    # Sort by mtime desc (newest first)
    ws_candidates.sort(key=lambda pair: getattr(pair[0], "mtime", 0), reverse=True)

    # Cap how many transcripts to read. Nothing is starved at this point -- the
    # workspace filter above ran over every provider's own window -- but parsing
    # transcripts is the expensive half, so read at most twenty times what the
    # table shows (floor of 50). Backseat sessions dominate a harness
    # workspace's history, so the cap has to sit well above ``limit``.
    ws_candidates = ws_candidates[: max(limit * 20, 50)]

    # Then read those transcripts newest-first in batches and stop as soon as
    # ``limit`` rows survive the backseat/empty filters -- a batched read,
    # never one row at a time (rationale at history.load_sessions). Stopping
    # early only skips rows this workspace's own newer sessions have already
    # filled the table with.
    results = []
    batch_size = max(limit * 3, 10)
    for start in range(0, len(ws_candidates), batch_size):
        chunk = ws_candidates[start : start + batch_size]
        try:
            texts = history.load_sessions([ref.key for ref, _ in chunk])
        except Exception:
            texts = {}

        for ref, project_dir in chunk:
            if len(results) >= limit:
                break

            text = texts.get(ref.key)
            # Drop sessions with no transcript or an empty prompt (owner brief).
            if text is None or not text.strip():
                continue

            if _is_backseat_session(text):
                continue

            prompt = _prompt_excerpt(text)
            if not prompt:
                continue

            # Create a preview (first 200 chars of content, stripped of markers)
            preview = text.strip()
            if len(preview) > 200:
                preview = preview[:200] + "…"

            results.append(ChatInfo(
                key=ref.key,
                native_id=getattr(ref, "session", "") or ref.key.partition(":")[2],
                project=project_dir,
                mtime=getattr(ref, "mtime", 0),
                preview=preview,
                prompt=prompt,
            ))

        if len(results) >= limit:
            break

    return results


def classify_session(text: str) -> str:
    """Classify a session transcript: "backseat task", "operator", or "other".

    - "backseat task": contains a START SESSION marker (marker.parse_marker)
    - "operator": contains an OPERATOR SESSION marker (marker.OPERATOR_SESSION_RE)
    - "other": neither marker present
    """
    if marker.parse_marker(text) is not None:
        return "backseat task"
    if marker.OPERATOR_SESSION_RE.search(text):
        return "operator"
    return "other"


def search_all_sessions(query: str = "", limit: int = 200) -> list[SessionSearchResult]:
    """Search all default providers, return sessions newest-first by mtime.

    Args:
        query: Search string passed to chat-mother-forker's find_chats (empty = all)
        limit: Maximum number of sessions to return

    Returns:
        List of SessionSearchResult sorted by mtime descending (newest first),
        each with a classification label.
    """
    try:
        from backseat_harness import history
    except ImportError:
        return []

    try:
        # One parse per row for the whole request: the search parses the rows
        # it returns (project lives on the conversation) and the transcript
        # fetch below reads them back from this same memoized list.
        find_chats = history._load("find_chats")
        providers = history.memoized_providers()
    except Exception:
        return []

    try:
        refs = find_chats(query, providers=providers, limit=limit)
    except Exception:
        return []

    # Sort by mtime desc (newest first)
    refs.sort(key=lambda r: getattr(r, "mtime", 0), reverse=True)

    # Load transcripts and classify
    results = []
    try:
        texts = history.load_sessions([r.key for r in refs], providers=providers)
    except Exception:
        texts = {}
    for ref in refs:
        if len(results) >= limit:
            break

        text = texts.get(ref.key)
        if text is None:
            classification = "other"
            preview = ""
        else:
            classification = classify_session(text)
            preview = text.strip()
            if len(preview) > 200:
                preview = preview[:200] + "…"

        results.append(SessionSearchResult(
            key=ref.key,
            native_id=getattr(ref, "session", "") or ref.key.partition(":")[2],
            project=getattr(ref, "project", None),
            mtime=getattr(ref, "mtime", 0),
            preview=preview,
            classification=classification,
        ))

    return results


# Map from chat-mother-forker provider key to backseat-harness fork provider name.
# Harness provider names match the registry keys in providers/__init__.py:
# 'opencode', 'kiro', 'claude'. Kiro IDE variants map to 'kiro' (kiro-cli tool).
# opencode -> 'opencode', claude_code -> 'claude'.
_CHAT_PROVIDER_TO_FORK_PROVIDER = {
    "opencode": "opencode",
    "kiro_cli": "kiro",
    "kiro_ide": "kiro",          # Kiro IDE -> kiro (harness provider)
    "kiro_ide_v2": "kiro",       # Kiro IDE v2 -> kiro
    "claude_code": "claude",
}

# Supported fork providers in scan order (plan §6 lines 110-111), using harness provider names.
_SUPPORTED_FORK_PROVIDERS = ("opencode", "kiro", "claude")


def _extract_provider_from_key(key: str) -> str | None:
    """Extract provider name from a ChatRef key (e.g., 'opencode:ses_...' -> 'opencode')."""
    if not key or ":" not in key:
        return None
    return key.split(":", 1)[0]


class _SessionKeyed(Protocol):
    """Structural type for what :func:`pick_fork_provider` reads: a session key.

    Lets the fork button pass the cheap ``history.SessionRef`` listing (which
    parses nothing) as well as full ``SessionSearchResult`` rows from the
    search page -- both carry ``key``, which is all the rules below look at.
    """

    key: str


def pick_fork_provider(current_session_key: str, results: Sequence[_SessionKeyed]) -> str:
    """Select the provider to use for forking a session.

    Rules (per 2-plan.md §6 lines 106-111):
    1. Current provider is Kiro IDE (kiro_ide or kiro_ide_v2) -> kiro
    2. Else, if results contain any supported-provider chat -> provider of the
       most recent such chat (by mtime).
    3. Else -> first of opencode / kiro / claude found on PATH.
    4. None on PATH -> raise RuntimeError.

    ``results`` is read newest-first; only ``key`` is consulted, so callers
    need no transcript text (the fork route lists keys only).
    """
    current_provider = _extract_provider_from_key(current_session_key)

    # Rule 1: Current provider is Kiro IDE -> kiro (harness provider)
    if current_provider in ("kiro_ide", "kiro_ide_v2"):
        return "kiro"

    # Rule 2: Most recent supported-provider chat in results
    for r in results:
        result_provider = _extract_provider_from_key(r.key)
        fork_provider = _CHAT_PROVIDER_TO_FORK_PROVIDER.get(result_provider)
        if fork_provider in _SUPPORTED_FORK_PROVIDERS:
            return fork_provider

    # Rule 3: PATH fallback in scan order
    _EXE_FOR = {"opencode": "opencode", "kiro": "kiro-cli", "claude": "claude"}
    for provider in _SUPPORTED_FORK_PROVIDERS:
        if shutil.which(_EXE_FOR[provider]):
            return provider

    # Rule 4: Nothing on PATH
    raise RuntimeError(
        f"no supported CLI found on PATH (looked for opencode, kiro-cli, claude); "
        f"current provider was {current_provider or 'unknown'}"
    )
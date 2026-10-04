"""Workspace chat search service (step 5.3, 6.1, 6.3).

Wraps chat-mother-forker search scoped to a workspace path, filtering out
backseat-managed sessions (those carrying <meta>START SESSION or
<meta>OPERATOR SESSION markers).
Also provides global session search and classification (step 6.1).
Also provides fork-provider selection helper (step 6.3).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from backseat_harness import marker


@dataclass(frozen=True)
class ChatInfo:
    """One chat session for the workspace page listing."""

    key: str
    native_id: str
    project: str | None
    mtime: float
    preview: str


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


def list_recent_chats(workspace: Path | str, limit: int = 20) -> list[ChatInfo]:
    """Recent chat sessions in this workspace, excluding backseat sessions.

    Scopes to the workspace by matching ChatRef.project against the workspace
    path (same logic as services.workspace._candidate_dirs). Loads each
    session's transcript to filter out backseat-marked sessions. Returns up to
    ``limit`` sessions, newest first (by mtime).
    """

    try:
        from backseat_harness import history
    except ImportError:
        return []

    try:
        find_chats = history._load("find_chats")
        default_providers = history._load("default_providers")
        providers = default_providers()
    except Exception:
        return []

    try:
        # Search with empty query to get all sessions, then filter by project
        refs = find_chats("", providers=providers, limit=limit * 3)  # fetch extra for filtering
    except Exception:
        return []

    # Filter by project matching workspace path
    ws_candidates = []
    for ref in refs:
        project = getattr(ref, "project", None)
        if project and _project_matches_workspace(project, Path(workspace)):
            ws_candidates.append(ref)

    # Sort by mtime desc (newest first)
    ws_candidates.sort(key=lambda r: getattr(r, "mtime", 0), reverse=True)

    # Load transcripts and filter out backseat sessions
    results = []
    load_chat = None
    for ref in ws_candidates:
        if len(results) >= limit:
            break

        try:
            if load_chat is None:
                load_chat = history._load("load_chat")
            text = load_chat(ref.key, providers=providers)
        except Exception:
            continue

        if _is_backseat_session(text):
            continue

        # Create a preview (first 200 chars of content, stripped of markers)
        preview = text.strip()
        if len(preview) > 200:
            preview = preview[:200] + "…"

        results.append(ChatInfo(
            key=ref.key,
            native_id=getattr(ref, "session", "") or ref.key.partition(":")[2],
            project=project,
            mtime=getattr(ref, "mtime", 0),
            preview=preview,
        ))

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
        find_chats = history._load("find_chats")
        default_providers = history._load("default_providers")
        providers = default_providers()
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
    load_chat = None
    for ref in refs:
        if len(results) >= limit:
            break

        try:
            if load_chat is None:
                load_chat = history._load("load_chat")
            text = load_chat(ref.key, providers=providers)
        except Exception:
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


def pick_fork_provider(current_session_key: str, results: list[SessionSearchResult]) -> str:
    """Select the provider to use for forking a session.

    Rules (per 2-plan.md §6 lines 106-111):
    1. Current provider is Kiro IDE (kiro_ide or kiro_ide_v2) -> kiro
    2. Else, if results contain any supported-provider chat -> provider of the
       most recent such chat (by mtime).
    3. Else -> first of opencode / kiro / claude found on PATH.
    4. None on PATH -> raise RuntimeError.
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
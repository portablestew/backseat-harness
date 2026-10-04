"""Workspace chat search service (step 5.3).

Wraps chat-mother-forker search scoped to a workspace path, filtering out
backseat-managed sessions (those carrying <meta>START SESSION or
<meta>OPERATOR SESSION markers).
"""

from __future__ import annotations

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
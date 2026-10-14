"""Workspace chat search service (step 5.3, 6.1, 6.3).

Wraps chat-mother-forker search scoped to a workspace path, filtering out
backseat-managed sessions (those carrying <meta>START SESSION or
<meta>OPERATOR SESSION markers).
Also provides global session search and classification (step 6.1).
Also provides fork-provider selection helper (step 6.3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from backseat_harness import marker
from backseat_harness.providers import binary_for, on_path_providers
from backseat_harness.web.services import task as task_service
from backseat_harness.web.services import workspace as workspace_service
from backseat_harness.web.services.workspace import _workspace_dir_for


@dataclass(frozen=True)
class ChatInfo:
    """One chat session for the workspace page listing."""

    key: str
    provider: str
    native_id: str
    project: str | None
    mtime: float
    preview: str
    prompt: str


_META_LINE_RE = re.compile(r"^\s*<meta>.*</meta>\s*$")
# chat_fork quotes USER/ASSISTANT lines with a leading blockquote marker; drop
# one marker (and a single following space) so the listing shows the prompt.
_QUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")


def _prompt_excerpt(text: str, limit: int = 120) -> str:
    """The chat's prompt string, per the owner brief: the first 120 characters
    of the user's prompt, with newlines and tabs replaced by spaces.

    1. Strip the blockquote marker from every line -- a managed transcript is
       stored as a quoted fork, so its ``<meta>`` lines read ``> <meta>…</meta>``
       -- and drop ``<meta>…</meta>`` marker lines wherever they appear: they
       are harness identity, never prompt text.
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

    lines = [
        line
        for line in (_QUOTE_PREFIX_RE.sub("", raw) for raw in text.splitlines())
        if not _META_LINE_RE.match(line)
    ]
    body = "\n".join(lines)

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
    prompt: str = ""  # parsed user-prompt excerpt (other chats)
    marker_id: str = ""  # backseat task root UUID (task chats)
    operator_slug: str = ""  # task slug (operator chats)


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
                provider=_extract_provider_from_key(ref.key) or "",
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
            prompt = ""
            marker_id = ""
            operator_slug = ""
        else:
            classification = classify_session(text)
            preview = text.strip()
            if len(preview) > 200:
                preview = preview[:200] + "…"
            prompt = _prompt_excerpt(text)
            parsed_marker = marker.parse_marker(text)
            marker_id = parsed_marker[0] if parsed_marker else ""
            operator_slug = marker.parse_operator_marker(text) or ""

        results.append(SessionSearchResult(
            key=ref.key,
            native_id=getattr(ref, "session", "") or ref.key.partition(":")[2],
            # Absolute workspace dir: ``ChatRef.project`` is only a basename,
            # so the dir is recovered from the provider's own store (the
            # homepage search resolves task hits to their lane through it).
            project=_ref_workspace_path(ref),
            mtime=getattr(ref, "mtime", 0),
            preview=preview,
            classification=classification,
            prompt=prompt,
            marker_id=marker_id,
            operator_slug=operator_slug,
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

# Harness provider names a fork can launch as; the guard in pick_fork_provider checks this.
_SUPPORTED_FORK_PROVIDERS = ("opencode", "kiro", "claude")


def _extract_provider_from_key(key: str) -> str | None:
    """Extract provider name from a ChatRef key (e.g., 'opencode:ses_...' -> 'opencode')."""
    if not key or ":" not in key:
        return None
    return key.split(":", 1)[0]


def pick_fork_provider(current_session_key: str, configured_provider: str) -> str:
    """Select the provider to use for forking a session.

    Rules (goal.md, "Provider selection"):
    1. Source chat is already a supported fork provider (opencode, kiro_cli,
       claude_code) -> its own fork provider via ``_CHAT_PROVIDER_TO_FORK_PROVIDER``.
    2. Source is Kiro IDE (kiro_ide or kiro_ide_v2) -> kiro (kiro-cli).
    3. Otherwise -> ``configured_provider``, the effective ``[harness]`` provider
       from ``load_config``.
    4. Guard: a configured provider outside ``_SUPPORTED_FORK_PROVIDERS``, or
       whose CLI is not on PATH, raises RuntimeError.

    Only the session key is read; no candidate list, so no recent-chat or PATH
    scan preference can override the configured provider.
    """
    current_provider = _extract_provider_from_key(current_session_key)

    # Rule 1: a source that is already a supported fork provider keeps its own.
    fork_provider = _CHAT_PROVIDER_TO_FORK_PROVIDER.get(current_provider)
    if fork_provider in _SUPPORTED_FORK_PROVIDERS:
        return fork_provider

    # Rule 2: Kiro IDE -> kiro (kiro-cli)
    if current_provider in ("kiro_ide", "kiro_ide_v2"):
        return "kiro"

    # Rule 3: the configured harness provider.
    # Rule 4 (guard): it must be a supported fork provider with its CLI on PATH.
    if configured_provider not in _SUPPORTED_FORK_PROVIDERS:
        raise RuntimeError(
            f"configured [harness] provider {configured_provider!r} cannot fork a chat; "
            f"expected one of {', '.join(_SUPPORTED_FORK_PROVIDERS)}"
        )
    if configured_provider not in on_path_providers():
        raise RuntimeError(
            f"configured [harness] provider {configured_provider!r} has no CLI on PATH "
            f"(looked for {binary_for(configured_provider)}); "
            f"current provider was {current_provider or 'unknown'}"
        )
    return configured_provider


@dataclass(frozen=True)
class HomepageResult:
    """One row for the homepage search table.

    Task rows carry ``status``/``badge`` for the status capsule; other-chat
    rows leave them empty and render a plain ``N/A``.
    """

    href: str
    prompt: str
    provider: str
    mtime: float
    status: str = ""
    badge: str = ""


def _ref_workspace_path(ref: object) -> str | None:
    """Absolute workspace dir for a search ref, or ``None`` when unrecoverable.

    ``ChatRef.project`` is only a directory *basename* (models.py), so the
    absolute dir is recovered from the provider's own store
    (``_workspace_dir_for``) -- a ``project`` that is already absolute is
    accepted as-is.
    """
    raw = _workspace_dir_for(ref)
    if raw is not None:
        return raw
    project = getattr(ref, "project", None)
    if project:
        path = Path(project).expanduser()
        if path.is_absolute():
            return str(path)
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _lane_index(workspace: str) -> dict[str, Path]:
    """Map a task's identities to its lane, for one workspace.

    ``binding.json`` is the authoritative source: it carries the lane's root
    UUID and every generation's native session id. The lane transcripts cannot
    serve here -- their markers are redacted to ``<task-root>`` on disk, so
    they never hold the real root UUID a chat's marker carries.
    """
    index: dict[str, Path] = {}
    tasks_dir = Path(workspace) / ".backseat" / "tasks"
    if not tasks_dir.is_dir():
        return index
    for lane in tasks_dir.iterdir():
        if not lane.is_dir():
            continue
        binding = _read_json(lane / "binding.json")
        if not isinstance(binding, dict):
            continue
        root = binding.get("root_uuid")
        if isinstance(root, str) and root:
            index.setdefault(root, lane)
        generations = binding.get("generations")
        if isinstance(generations, list):
            for gen in generations:
                native = gen.get("native_id") if isinstance(gen, dict) else None
                if isinstance(native, str) and native:
                    index.setdefault(native, lane)
    return index


def _task_lane(
    result: SessionSearchResult, lane_index: dict[str, dict[str, Path]]
) -> Path | None:
    """The lane for a task hit, looked up in ``lane_index``.

    The index is built once per workspace (keyed by its normalized path) from
    the lanes' ``binding.json`` -- small reads, where re-scanning transcripts
    per hit would re-read megabytes.
    """
    project = result.project
    if not project:
        return None
    key = workspace_service._normalize(project)
    index = lane_index.get(key)
    if index is None:
        index = _lane_index(project)
        lane_index[key] = index
    return index.get(result.marker_id) or index.get(result.native_id)


def _task_row(
    result: SessionSearchResult, lane: Path | None, ws_paths: dict[str, int]
) -> HomepageResult:
    """Enrich a task hit: resolve its lane for the goal prompt and status.

    Falls back to the chat link and the chat's parsed prompt when the lane
    (or its registered workspace) cannot be resolved.
    """
    provider = _extract_provider_from_key(result.key) or ""
    if lane is None:
        return HomepageResult(
            href=f"/chat/{provider}/{result.native_id}",
            prompt=result.prompt,
            provider=provider,
            mtime=result.mtime,
        )

    ws_id = ws_paths.get(workspace_service._normalize(result.project))
    info = task_service._lane_info(lane)
    href = (
        f"/ws/{ws_id}/tasks/{lane.name}"
        if ws_id is not None
        else f"/chat/{provider}/{result.native_id}"
    )
    return HomepageResult(
        href=href,
        prompt=_prompt_excerpt(_read_text(lane / "goal.md")),
        provider=lane.name,
        mtime=result.mtime,
        status=info.status,
        badge=info.badge,
    )


def _operator_lane(result: SessionSearchResult) -> Path | None:
    """The task lane an operator-session hit belongs to, or None.

    The operator tag's slug names the lane; the lane-existence check also
    disambiguates a task-less operator tag, whose slug position holds the
    phase instead.
    """
    project = result.project
    if not project or not result.operator_slug:
        return None
    lane = Path(project) / ".backseat" / "tasks" / result.operator_slug
    return lane if lane.is_dir() else None


def homepage_search(query: str = "", limit: int = 20) -> list[HomepageResult]:
    """Homepage search rows: task chats (deduped, enriched) interleaved with
    other chats in chat-search order.

    All hits for one task collapse into a single row -- managed chats (root
    UUID marker) and operator chats (slug marker) alike -- keyed by the task's
    lane, keeping the first hit's sort position and metadata. An operator hit
    whose task lane no longer resolves is harness-internal and is dropped.
    """
    # Extra raw results so task dedup still fills the row cap.
    raw = search_all_sessions(query, limit * 3)

    ws_paths = {
        workspace_service._normalize(p): ws_id
        for ws_id, p in workspace_service.list_workspaces_with_ids()
    }
    lane_index: dict[str, dict[str, Path]] = {}

    seen_tasks: set[tuple[str, str]] = set()
    rows: list[HomepageResult] = []
    for result in raw:
        if len(rows) >= limit:
            break

        if result.classification == "backseat task":
            lane = _task_lane(result, lane_index)
            if lane is None:
                # An unresolvable task hit is still a real user chat: keep it
                # as a plain chat row rather than dropping it.
                rows.append(_task_row(result, None, ws_paths))
                continue
            key = (workspace_service._normalize(result.project), lane.name)
            if key in seen_tasks:
                continue
            seen_tasks.add(key)
            rows.append(_task_row(result, lane, ws_paths))
        elif result.classification == "operator":
            lane = _operator_lane(result)
            if lane is None:
                continue
            key = (workspace_service._normalize(result.project), lane.name)
            if key in seen_tasks:
                continue
            seen_tasks.add(key)
            rows.append(_task_row(result, lane, ws_paths))
        else:
            provider = _extract_provider_from_key(result.key) or ""
            rows.append(
                HomepageResult(
                    href=f"/chat/{provider}/{result.native_id}",
                    prompt=result.prompt,
                    provider=provider,
                    mtime=result.mtime,
                )
            )
    return rows
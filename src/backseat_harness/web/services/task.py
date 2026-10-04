"""Workspace-page task service (step 5.1).

Wraps the seams from 2-plan.md §5 so routes/templates stay thin:

- :func:`sanitize_slug` — task name → valid dirname (``[a-z0-9_-]+``).
- :func:`create_task` — writes **only** ``.backseat/tasks/<slug>/goal.md``
  (V8/V29: no ``state.json``/``checklist.json``; the daemon picks the lane
  up at the next check-in, and ``claim_task`` auto-registers the workspace
  on first run — no CLI call needed).
- :func:`list_incomplete` / :func:`list_recent` — filesystem scan of the
  workspace's ``tasks/`` dir joined with ``storage.load_state`` for status,
  sorted by ``state.updated_at`` desc; a lane with no ``state.json`` yet
  reads as ``continue`` with the ``goal.md`` mtime.
- :func:`resolve_workspace` — sqlite ``workspaces.id`` → path (stable
  across path renames).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from backseat_harness import storage
from backseat_harness.config import TaskPaths
from backseat_harness.models import TaskStatus

#: A sanitized slug is a valid directory name: lowercase alphanumerics,
#: dash, underscore (2-plan.md §5).
_SLUG_CHARS = re.compile(r"[^a-z0-9_-]+")


def sanitize_slug(name: str) -> str:
    """Task name → filesystem-safe slug (V9).

    ``"My Task!"`` → ``"my-task"``; ``"a/b"`` → ``"a-b"``; Unicode is
    NFKD-folded to ASCII (``"Ünïcodé"`` → ``"unicode"``). Raises
    ``ValueError`` when nothing usable remains.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_CHARS.sub("-", folded.lower()).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or slug in (".", ".."):
        raise ValueError(f"task name {name!r} has no usable slug characters")
    return slug


def create_task(workspace: Path | str, name: str, goal: str) -> str:
    """Create ``.backseat/tasks/<slug>/goal.md`` with ``goal``; return slug.

    Writes the goal file only — never ``state.json``/``checklist.json``
    (V29). Raises ``ValueError`` for an unusable name and
    ``FileExistsError`` when the lane's ``goal.md`` already exists.
    """
    slug = sanitize_slug(name)
    task_dir = Path(workspace).expanduser().resolve() / ".backseat" / "tasks" / slug
    task_dir.mkdir(parents=True, exist_ok=True)
    goal_path = task_dir / "goal.md"
    if goal_path.exists():
        raise FileExistsError(f"task {slug!r} already exists at {goal_path}")
    goal_path.write_text((goal or "").rstrip() + "\n", encoding="utf-8")
    return slug


def badge_for(status: TaskStatus | str) -> str:
    """Status badge text: blocked → "needs attention", else "in-progress"."""
    value = status.value if isinstance(status, TaskStatus) else str(status)
    if value == TaskStatus.BLOCKED.value:
        return "needs attention"
    if value == TaskStatus.COMPLETE.value:
        return "complete"
    return "in-progress"


@dataclass(frozen=True)
class TaskInfo:
    """One task lane for the workspace page listings."""

    slug: str
    status: str
    badge: str
    updated_at: float


def _lane_info(task_dir: Path) -> TaskInfo:
    slug = task_dir.name
    goal_path = task_dir / "goal.md"
    try:
        state = storage.load_state(TaskPaths(task_dir), slug)
        status = state.status
        updated_at = state.updated_at
        if not (task_dir / "state.json").exists():
            try:
                updated_at = goal_path.stat().st_mtime
            except OSError:
                pass
    except Exception:
        status = TaskStatus.CONTINUE
        try:
            updated_at = goal_path.stat().st_mtime
        except OSError:
            updated_at = task_dir.stat().st_mtime
    value = status.value if isinstance(status, TaskStatus) else str(status)
    return TaskInfo(slug=slug, status=value, badge=badge_for(value), updated_at=updated_at)


def list_tasks(workspace: Path | str) -> list[TaskInfo]:
    """Every task lane in the workspace, newest ``updated_at`` first."""
    tasks_dir = Path(workspace).expanduser().resolve() / ".backseat" / "tasks"
    if not tasks_dir.is_dir():
        return []
    lanes = [p for p in tasks_dir.iterdir() if p.is_dir()]
    infos = [_lane_info(p) for p in lanes]
    infos.sort(key=lambda i: (i.updated_at, i.slug), reverse=True)
    return infos


def list_incomplete(workspace: Path | str) -> list[TaskInfo]:
    """Lanes whose status is not ``complete`` (V10 table)."""
    return [i for i in list_tasks(workspace) if i.status != TaskStatus.COMPLETE.value]


def list_recent(workspace: Path | str, limit: int = 20) -> list[TaskInfo]:
    """All lanes, newest first, capped at ``limit`` (V11)."""
    return list_tasks(workspace)[:limit]


def resolve_workspace(ws_id: int) -> Path | None:
    """Workspace path for a sqlite ``workspaces.id`` (URL key), or None."""
    from backseat_harness import db

    try:
        ws_int = int(ws_id)
    except (TypeError, ValueError):
        return None
    with db.open_db() as conn:
        row = conn.execute(
            "SELECT path FROM workspaces WHERE id = ?", (ws_int,)
        ).fetchone()
    return Path(row["path"]) if row is not None else None

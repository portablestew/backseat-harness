"""Workspace-page task service (step 5.1, 7.1, 7.3).

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
- :func:`load_task` — loads goal.md, state.json, checklist.json, and any
  transcript.md/logs for the read-only task viewer (step 7.1).
- :func:`run_task_detached` — launches ``backseat-harness run <slug> [brief]``
  for the task in a detached subprocess (step 7.3).
"""

from __future__ import annotations

import json
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


def _raw_state_status(task_dir: Path) -> str | None:
    """Raw ``status`` string from ``state.json`` if it parses as JSON.

    ``TaskState.from_dict`` raises when a status value is not one of the enum
    members, which would coerce the lane to ``continue`` and hide the real
    value from the ``data-status`` hook; this fallback keeps unknown statuses
    visible (goal G5: unknown keeps the grey badge default).
    """
    try:
        raw = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    value = raw.get("status") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


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
        status = _raw_state_status(task_dir) or TaskStatus.CONTINUE
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
    """Active lanes: every lane whose status is not ``complete`` (V10 table,
    uncapped -- the original design lists all incomplete work)."""
    return [i for i in list_tasks(workspace) if i.status != TaskStatus.COMPLETE.value]


def list_complete(workspace: Path | str) -> list[TaskInfo]:
    """Completed lanes: the analogue of ``list_incomplete`` for the
    "Completed tasks" section (goal G6)."""
    return [i for i in list_tasks(workspace) if i.status == TaskStatus.COMPLETE.value]


def list_recent(workspace: Path | str, limit: int = 10) -> list[TaskInfo]:
    """The newest lanes across statuses, capped (V11; 10 per goal G6)."""
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


@dataclass(frozen=True)
class TaskDetail:
    """Complete task data for the read-only viewer (step 7.1)."""

    slug: str
    goal: str
    status: str
    badge: str
    iteration: int
    generation: int
    reason: str
    updated_at: float
    checklist: dict
    remarks: list[str]
    transcript: str | None
    log_files: list[str]


def load_task(workspace: Path | str, slug: str) -> TaskDetail | None:
    """Load all task lane files for the read-only viewer.

    Returns None if the task lane doesn't exist.
    """
    ws_path = Path(workspace).expanduser().resolve()
    task_dir = ws_path / ".backseat" / "tasks" / slug
    if not task_dir.is_dir():
        return None

    goal_path = task_dir / "goal.md"
    checklist_path = task_dir / "checklist.json"
    transcript_path = task_dir / "transcript.md"
    logs_dir = task_dir / "logs"

    # Load goal.md (always exists if task dir exists)
    try:
        goal = goal_path.read_text(encoding="utf-8").strip()
    except OSError:
        goal = ""

    # Load state.json
    try:
        from backseat_harness import storage
        state = storage.load_state(TaskPaths(task_dir), slug)
        status = state.status.value if hasattr(state.status, "value") else str(state.status)
        iteration = state.iteration
        generation = state.generation
        reason = state.reason
        updated_at = state.updated_at
    except Exception:
        status = _raw_state_status(task_dir) or TaskStatus.CONTINUE.value
        iteration = 0
        generation = 1
        reason = ""
        try:
            updated_at = goal_path.stat().st_mtime
        except OSError:
            updated_at = task_dir.stat().st_mtime

    # Load checklist.json
    try:
        from backseat_harness import checklist as checklist_mod
        checklist_obj = checklist_mod.load_checklist(checklist_path)
        checklist_data = checklist_obj.to_dict()
    except Exception:
        checklist_data = {"steps": {}, "remarks": []}

    # Load transcript.md
    transcript = None
    if transcript_path.exists():
        try:
            transcript = transcript_path.read_text(encoding="utf-8")
        except OSError:
            pass

    # List log files
    log_files = []
    if logs_dir.is_dir():
        try:
            log_files = sorted(
                [f.name for f in logs_dir.iterdir() if f.is_file()],
                key=lambda f: (logs_dir / f).stat().st_mtime,
                reverse=True,
            )
        except OSError:
            pass

    return TaskDetail(
        slug=slug,
        goal=goal,
        status=status,
        badge=badge_for(status),
        iteration=iteration,
        generation=generation,
        reason=reason,
        updated_at=updated_at,
        checklist=checklist_data.get("steps", {}),
        remarks=checklist_data.get("remarks", []),
        transcript=transcript,
        log_files=log_files,
    )


def run_task_detached(workspace: Path | str, slug: str, brief: str | None = None) -> None:
    """Launch ``backseat-harness run <slug> [brief]`` as a detached subprocess.

    The subprocess is started with the workspace as its cwd and its output
    discarded (or could be piped to a log file). The call returns immediately
    without waiting for the run to complete.

    Args:
        workspace: Workspace root path (containing .backseat/)
        slug: Task slug to run
        brief: Optional brief text to pass to the run command
    """
    import subprocess
    import sys

    ws_path = Path(workspace).expanduser().resolve()

    # Build the command: python -m backseat_harness.cli run <slug> [brief]
    # Using -m so it uses the same interpreter as the web server
    cmd = [sys.executable, "-m", "backseat_harness.cli", "-C", str(ws_path), "run", slug]
    if brief:
        cmd.append(brief)

    # Start detached - on Windows use CREATE_NEW_PROCESS_GROUP, on Unix use start_new_session
    if sys.platform == "win32":
        subprocess.Popen(
            cmd,
            cwd=str(ws_path),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            cmd,
            cwd=str(ws_path),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

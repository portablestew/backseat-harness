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
  reads as ``pending`` with the ``goal.md`` mtime.
- :func:`resolve_workspace` — sqlite ``workspaces.id`` → path (stable
  across path renames).
- :func:`load_task` — loads goal.md, state.json, checklist.json, and any
  transcript.md for the read-only task viewer (step 7.1).
- :func:`run_task_detached` — launches ``backseat-harness run <slug> [brief]``
  for the task in a detached subprocess (step 7.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backseat_harness import manager, storage
from backseat_harness.config import (
    DEFAULT_MANAGER_PROVIDER,
    STAGING_PREFIX,
    TaskPaths,
    is_task_lane,
    load_config,
    sanitize_slug,
)
from backseat_harness.models import TaskStatus


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


#: Status of a lane that has no ``state.json`` yet: it has never run.
PENDING_STATUS = "pending"


def status_value(task_dir: Path, status: TaskStatus | str) -> str:
    """The lane's status string, the one derivation every listing and page uses.

    ``storage.load_state`` answers a missing ``state.json`` with the
    ``TaskState`` default (``continue``); that default must not leak through,
    so a lane with no ``state.json`` reads :data:`PENDING_STATUS` instead.
    """
    if not (task_dir / "state.json").exists():
        return PENDING_STATUS
    return status.value if isinstance(status, TaskStatus) else str(status)


def badge_for(status: TaskStatus | str) -> str:
    """Status badge text: blocked → "needs attention", complete → "complete",
    continue → "in progress"; any other status keeps its own label."""
    value = status.value if isinstance(status, TaskStatus) else str(status)
    if value == TaskStatus.BLOCKED.value:
        return "needs attention"
    if value == TaskStatus.COMPLETE.value:
        return "complete"
    if value == TaskStatus.CONTINUE.value:
        return "in progress"
    return value


def run_action_for(task_dir: Path, status: str) -> str:
    """Verb for the Run block, from the lane's state (goal.md:35).

    A lane with no ``state.json`` has never run, so the control starts it
    ("Run"). ``continue`` means it is still in flight, so running again
    interrupts it with the new brief ("Interrupt"). Anything else is restarted
    ("Restart"). One rule feeds both the label and the button so they cannot
    drift.
    """
    if not (task_dir / "state.json").exists():
        return "Run"
    if status == TaskStatus.CONTINUE.value:
        return "Interrupt"
    return "Restart"


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
    value = status_value(task_dir, status)
    return TaskInfo(slug=slug, status=value, badge=badge_for(value), updated_at=updated_at)


def list_tasks(workspace: Path | str) -> list[TaskInfo]:
    """Every task lane in the workspace, newest ``updated_at`` first."""
    tasks_dir = Path(workspace).expanduser().resolve() / ".backseat" / "tasks"
    if not tasks_dir.is_dir():
        return []
    lanes = [p for p in tasks_dir.iterdir() if is_task_lane(p)]
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


def list_recent_task_dirs(
    workspace: Path | str, limit: int = 3
) -> tuple[list[str], int]:
    """Newest task-lane directory names by the directory's **own** mtime.

    Returns ``(slugs, more)``: up to ``limit`` slugs, newest first, plus how
    many lanes were left out. The homepage "Tasks" column orders by the task
    directory's ``st_mtime`` as the goal asks, not ``TaskInfo.updated_at``
    (goal.md:25).
    """
    tasks_dir = Path(workspace).expanduser().resolve() / ".backseat" / "tasks"
    if not tasks_dir.is_dir():
        return [], 0
    lanes: list[tuple[float, str]] = []
    for lane in tasks_dir.iterdir():
        if not is_task_lane(lane):
            continue
        try:
            mtime = lane.stat().st_mtime
        except OSError:
            continue
        lanes.append((mtime, lane.name))
    lanes.sort(key=lambda item: (item[0], item[1]), reverse=True)
    slugs = [name for _, name in lanes]
    return slugs[:limit], max(0, len(slugs) - limit)


#: Badge text for each :func:`manager.generation_state` value.
GENERATION_BADGES = {
    "generating": "generating",
    "failed": "generation failed",
    "interrupted": "interrupted",
}

#: Reason shown on an interrupted row when the manifest has none.
INTERRUPTED_REASON = "the generator stopped before it finished"


def list_generating(workspace: Path | str) -> list[dict]:
    """Generation staging dirs to show above the task lanes, newest first.

    Each dict has ``name`` (the ``_gen-*`` dir), ``status`` and ``badge`` (from
    :data:`GENERATION_BADGES`), ``reason``, ``source_key``, and ``created_at``.
    A staging dir has no task page, so these are plain rows, not links. A dir
    with no readable manifest, or one :func:`manager.generation_state` leaves
    out, is skipped.
    """
    tasks_dir = Path(workspace).expanduser().resolve() / ".backseat" / "tasks"
    if not tasks_dir.is_dir():
        return []
    rows: list[dict] = []
    for staging in tasks_dir.glob(f"{STAGING_PREFIX}*"):
        if not staging.is_dir():
            continue
        manifest = manager.read_manifest(staging)
        status = manager.generation_state(manifest) if manifest is not None else None
        if status is None:
            continue
        reason = str(manifest.get("reason") or "")
        if status == "interrupted" and not reason:
            reason = INTERRUPTED_REASON
        created = manifest.get("created_at")
        source_key = str(manifest.get("source_key") or "")
        provider, _, conversation_id = source_key.partition(":")
        rows.append(
            {
                "name": staging.name,
                "status": status,
                "badge": GENERATION_BADGES[status],
                "reason": reason,
                "source_key": source_key,
                "provider": provider,
                "conversation_id": conversation_id,
                "created_at": float(created) if isinstance(created, int | float) else 0.0,
            }
        )
    rows.sort(key=lambda row: (row["created_at"], row["name"]), reverse=True)
    return rows


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
    run_action: str
    provider: str
    iteration: int
    generation: int
    reason: str
    updated_at: float
    checklist: dict
    remarks: list[str]
    transcript: str | None


def _task_provider(ws_path: Path, task_dir: Path) -> str:
    """The provider that owns this lane, for the Status table's first row.

    A lane's ``binding.json`` is authoritative when present -- it records which
    CLI owns the lane's session chain (same precedence as
    ``cli._managed_provider``). A never-run lane has no binding, so it falls
    back to the workspace's configured ``[harness]`` provider; an unreadable
    binding or config resolves to the manager default rather than failing the
    page.
    """
    try:
        binding = storage.load_binding(TaskPaths(task_dir))
        if binding is not None and binding.provider:
            return binding.provider
    except Exception:
        pass
    try:
        return load_config(ws_path).provider or DEFAULT_MANAGER_PROVIDER
    except Exception:
        return DEFAULT_MANAGER_PROVIDER


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

    # Load goal.md (always exists if task dir exists)
    try:
        goal = goal_path.read_text(encoding="utf-8").strip()
    except OSError:
        goal = ""

    # Load state.json
    try:
        from backseat_harness import storage
        state = storage.load_state(TaskPaths(task_dir), slug)
        status = status_value(task_dir, state.status)
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

    return TaskDetail(
        slug=slug,
        goal=goal,
        status=status,
        badge=badge_for(status),
        run_action=run_action_for(task_dir, status),
        provider=_task_provider(ws_path, task_dir),
        iteration=iteration,
        generation=generation,
        reason=reason,
        updated_at=updated_at,
        checklist=checklist_data.get("steps", {}),
        remarks=checklist_data.get("remarks", []),
        transcript=transcript,
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


def generate_task_detached(workspace: Path | str, source_key: str, staging_name: str):
    """Launch ``backseat-harness generate-task`` for a chat as a detached subprocess.

    ``staging_name`` is the ``_gen-*`` dir the route already created with its
    ``queued`` manifest. The child writes its own pid into it first. Stdio
    is discarded and stdin is closed, because the child builds its own prompts.
    The Popen handle is returned but never waited on, so the request returns as
    soon as the child has started.
    """
    import subprocess
    import sys

    ws_path = Path(workspace).expanduser().resolve()
    cmd = [
        sys.executable,
        "-m",
        "backseat_harness.cli",
        "-C",
        str(ws_path),
        "generate-task",
        source_key,
        "--staging",
        staging_name,
    ]
    if sys.platform == "win32":
        return subprocess.Popen(
            cmd,
            cwd=str(ws_path),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(
        cmd,
        cwd=str(ws_path),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

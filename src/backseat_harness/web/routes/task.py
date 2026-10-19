"""Task viewer route (step 7.1, 7.3, 7.4): GET /ws/<ws_id>/tasks/<slug>,
POST /ws/<ws_id>/tasks/<slug>/run.

Thin over ``services.task.load_task`` and ``services.task.run_task_detached``.
Unknown ws_id or slug returns 404.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backseat_harness.web.services import task as task_service

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_dt(ts: float) -> str:
    """Format timestamp as YYYY-MM-DD HH:MM."""
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


templates.env.filters["format_datetime"] = _fmt_dt


def _resolve_or_404(ws_id: int) -> Path | None:
    """Resolve workspace path or return None for 404."""
    return task_service.resolve_workspace(ws_id)


def _build_takeover_command(ws_path: Path, slug: str) -> list[str]:
    """Build the takeover command: python -m backseat_harness.cli -C <ws> takeover <slug>."""
    return [sys.executable, "-m", "backseat_harness.cli", "-C", str(ws_path), "takeover", slug]


@router.get("/ws/{ws_id}/tasks/{slug}", response_class=HTMLResponse)
def task_page(request: Request, ws_id: int, slug: str):
    """Read-only task viewer: goal, status, iteration, generation, checklist.

    Reads only; the page writes nothing.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    task = task_service.load_task(ws_path, slug)
    if task is None:
        return HTMLResponse("Task not found", status_code=404)

    # Build takeover command for the Takeover button
    takeover_cmd = _build_takeover_command(ws_path, slug)

    return templates.TemplateResponse(
        request,
        "task.html",
        {
            "ws_id": ws_id,
            "ws_name": ws_path.name,
            "task": task,
            "takeover_cmd": takeover_cmd,
        },
    )


@router.post("/ws/{ws_id}/tasks/{slug}/run", response_class=HTMLResponse)
def task_run(request: Request, ws_id: int, slug: str, brief: str = Form(default="")):
    """Launch a detached run for this task and redirect straight back to the page.

    A brief means the owner is redirecting the lane, so the run passes
    ``--interrupt`` and stops a live harness first. The run is not awaited: a
    refusal or crash is written to the lane's daemon.log, and the Status table's
    Uptime row shows whether a harness took the lane.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    task = task_service.load_task(ws_path, slug)
    if task is None:
        return HTMLResponse("Task not found", status_code=404)

    brief = brief.strip()
    task_service.run_task_detached(ws_path, slug, brief or None, interrupt=bool(brief))
    return RedirectResponse(url=f"/ws/{ws_id}/tasks/{slug}", status_code=303)
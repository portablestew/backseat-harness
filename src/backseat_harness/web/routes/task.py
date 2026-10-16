"""Task viewer route (step 7.1, 7.3, 7.4): GET /ws/<ws_id>/tasks/<slug>,
POST /ws/<ws_id>/tasks/<slug>/run.

Thin over ``services.task.load_task`` and ``services.task.run_task_detached``.
Unknown ws_id or slug returns 404.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode

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


#: How long the Run route waits for the detached run to take the lane: the CLI's
#: stop grace (cli._KILL_GRACE_S, 5 s) plus 5 s for the claim after it.
_START_WAIT_S = 10.0


def _build_takeover_command(ws_path: Path, slug: str) -> list[str]:
    """Build the takeover command: python -m backseat_harness.cli -C <ws> takeover <slug>."""
    return [sys.executable, "-m", "backseat_harness.cli", "-C", str(ws_path), "takeover", slug]


@router.get("/ws/{ws_id}/tasks/{slug}", response_class=HTMLResponse)
def task_page(request: Request, ws_id: int, slug: str):
    """Read-only task viewer: goal, status, iteration, generation, checklist.

    A ``notice`` query parameter (set by a failed Run) is shown as a banner above
    the Run form. The template escapes it, so the text is shown as written.
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
            "notice": request.query_params.get("notice"),
        },
    )


@router.get("/ws/{ws_id}/tasks/{slug}/transcript", response_class=HTMLResponse)
def task_transcript(request: Request, ws_id: int, slug: str):
    """Standalone transcript page for a task lane (step 6d).

    Shares the chat page's layout (``_chat_page.html``): a sticky header whose
    back link returns to the task page, and the full transcript below. Unknown
    workspace or task slugs 404 like every other task route.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    task = task_service.load_task(ws_path, slug)
    if task is None:
        return HTMLResponse("Task not found", status_code=404)

    return templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "ws_id": ws_id,
            "ws_name": ws_path.name,
            "task": task,
            "transcript": task.transcript or "",
        },
    )


@router.post("/ws/{ws_id}/tasks/{slug}/run", response_class=HTMLResponse)
def task_run(request: Request, ws_id: int, slug: str, brief: str = Form(default="")):
    """Launch a detached run for this task, then wait briefly for it to take the lane.

    A brief means the owner is redirecting the lane, so the run passes
    ``--interrupt`` and stops a live harness first. The redirect goes back to the
    task page. A run that fails to start, or is slow to, adds a ``notice`` with
    the reason, so the page says why instead of showing nothing.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    task = task_service.load_task(ws_path, slug)
    if task is None:
        return HTMLResponse("Task not found", status_code=404)

    brief = brief.strip()
    proc, log_path, offset = task_service.run_task_detached(
        ws_path, slug, brief or None, interrupt=bool(brief)
    )
    notice = task_service.run_start_notice(
        ws_path, slug, proc, log_path, offset, timeout_s=_START_WAIT_S
    )

    # Redirect back to the task page, with the reason when the run did not start
    url = f"/ws/{ws_id}/tasks/{slug}"
    if notice:
        url += "?" + urlencode({"notice": notice})
    return RedirectResponse(url=url, status_code=303)
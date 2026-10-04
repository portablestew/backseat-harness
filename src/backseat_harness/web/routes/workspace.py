"""Workspace page routes (step 5.2-5.3): GET /ws/<ws_id>, POST /ws/<ws_id>/tasks.

Thin over ``services.task`` and ``services.chat_search``: rendering lives here.
Unknown ws_id returns 404.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import task as task_service

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

def _fmt_dt(ts: float) -> str:
    """Format timestamp as YYYY-MM-DD HH:MM."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# Custom filter: timestamp -> human-readable
templates.env.filters["format_datetime"] = _fmt_dt


def _resolve_or_404(ws_id: int) -> Path | None:
    """Resolve workspace path or return None for 404."""
    return task_service.resolve_workspace(ws_id)


@router.get("/ws/{ws_id}", response_class=HTMLResponse)
def workspace_page(request: Request, ws_id: int):
    """Workspace page: header, add-task form, incomplete table, recent tasks, recent chats."""
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    name = ws_path.name
    incomplete = task_service.list_incomplete(ws_path)
    recent = task_service.list_recent(ws_path)
    recent_chats = chat_search_service.list_recent_chats(ws_path)

    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "ws_id": ws_id,
            "ws_name": name,
            "ws_path": str(ws_path),
            "incomplete": incomplete,
            "recent": recent,
            "recent_chats": recent_chats,
        },
    )


@router.post("/ws/{ws_id}/tasks", response_class=HTMLResponse)
def add_task(ws_id: int, name: str = Form(...), goal: str = Form(...)):
    """Create a task lane; on success redirect to workspace page, else toast error."""
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    try:
        task_service.create_task(ws_path, name, goal)
    except ValueError as e:
        return HTMLResponse(f"<p><strong>error:</strong> {e}</p>", status_code=400)
    except FileExistsError as e:
        return HTMLResponse(f"<p><strong>error:</strong> {e}</p>", status_code=400)

    return RedirectResponse(url=f"/ws/{ws_id}", status_code=303)
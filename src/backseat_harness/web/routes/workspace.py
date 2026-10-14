"""Workspace page routes (step 5.2-5.3): GET /ws/<ws_id>, POST /ws/<ws_id>/tasks,
POST /ws/<ws_id>/chats (new-chat-button step 3).

Thin over ``services.task`` and ``services.chat_search``: rendering lives here.
Unknown ws_id returns 404.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backseat_harness.providers import (
    ProviderError,
    available_providers,
    binary_for,
    get_provider,
    on_path_providers,
)
from backseat_harness.spawn import resolve_argv
from backseat_harness.web.routes.shell import get_manager
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
    """Workspace page: Active/Completed tasks and one capped "Other chats" list.

    One ``list_recent_chats`` fetch feeds the single non-task "Other chats"
    table (ten rows max, plan 3e superseded by the owner brief): there is no
    separate "Recent chats" listing and no expandable overflow, and the old
    "Recent tasks" section (which duplicated "Completed tasks") is gone.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    name = ws_path.name
    generating = task_service.list_generating(ws_path)
    incomplete = task_service.list_incomplete(ws_path)
    complete = task_service.list_complete(ws_path)
    chats = chat_search_service.list_recent_chats(ws_path, limit=10)
    new_chat_providers = [
        {"name": p, "label": binary_for(p)} for p in on_path_providers()
    ]

    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "ws_id": ws_id,
            "ws_name": name,
            "ws_path": str(ws_path),
            "generating": generating,
            "incomplete": incomplete,
            "complete": complete,
            "chats": chats,
            "new_chat_providers": new_chat_providers,
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


@router.post("/ws/{ws_id}/chats", response_class=HTMLResponse)
def new_chat(ws_id: int, provider: str = Form(...)):
    """Start a fresh chat in a shell at the workspace; 303 to the shell page.

    The argv is the provider's own fresh-chat command, resolved through PATH
    (``resolve_argv``) so npm ``.cmd`` shims launch on Windows. Unknown or
    absent providers are rejected before any PTY is spawned.
    """
    ws_path = _resolve_or_404(ws_id)
    if ws_path is None:
        return HTMLResponse("Workspace not found", status_code=404)

    if provider not in available_providers():
        return HTMLResponse(
            f"<p><strong>error:</strong> unknown provider {provider!r}</p>",
            status_code=400,
        )
    if provider not in on_path_providers():
        return HTMLResponse(
            f"<p><strong>error:</strong> {provider} is not on PATH</p>",
            status_code=400,
        )

    try:
        argv = resolve_argv(get_provider(provider).interactive_command(None))
    except ProviderError as e:
        return HTMLResponse(f"<p><strong>error:</strong> {e}</p>", status_code=400)

    session_id = get_manager().create_session(argv, ws_path)
    title = f"New {ws_path.name} {binary_for(provider)}"
    params = urlencode({"from": f"/ws/{ws_id}", "label": ws_path.name, "title": title})
    return RedirectResponse(url=f"/shell/{session_id}?{params}", status_code=303)
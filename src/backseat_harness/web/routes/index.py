"""Homepage routes (step 4.3): workspace list, add, fragment, suggest.

Thin over ``services.workspace``: rendering lives here, registry/chat
seams stay in the service layer. Snippet endpoints return small HTML
fragments for HTMX targets (``#toast``, ``#workspace-list``,
``#suggestions``).
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backseat_harness.web.services import workspace as workspace_service

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_POLL_ATTRS = (
    'hx-get="/workspaces/fragment" hx-trigger="every 60s" hx-swap="outerHTML"'
)


def _entries() -> list[dict[str, str]]:
    """Workspace rows for templates: trailing name + full path (V4)."""
    return [
        {"name": workspace_service.display_name(p), "path": p}
        for p in workspace_service.list_workspaces()
    ]


def _toast_html(ok: bool, message: str) -> str:
    kind = "ok" if ok else "error"
    return f"<p><strong>{kind}:</strong> {html.escape(message)}</p>"


def _fragment_html(entries: list[dict[str, str]]) -> str:
    if entries:
        cards = "\n".join(
            "    <article class=\"workspace-card\">\n"
            f"      <strong>{html.escape(e['name'])}</strong>"
            f"<br><small>{html.escape(e['path'])}</small>\n"
            "    </article>"
            for e in entries
        )
    else:
        cards = "    <p>No workspaces registered yet.</p>"
    return f"<div id=\"workspace-list\" {_POLL_ATTRS}>\n{cards}\n</div>"


def _suggest_html(suggestions: list[str]) -> str:
    if not suggestions:
        return "<p>No unregistered workspace suggestions found.</p>"
    parts = []
    for s in suggestions:
        vals = json.dumps({"path": s}).replace("'", "&#x27;")
        parts.append(
            f"<button hx-post=\"/workspaces\" hx-vals='{vals}' "
            f"hx-target=\"#toast\" hx-swap=\"innerHTML\">"
            f"{html.escape(s)}</button>"
        )
    return "\n".join(parts)


@router.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    """Full homepage: workspace cards, add form, suggest button."""
    return templates.TemplateResponse(
        request, "index.html", {"workspaces": _entries(), "suggestions": []}
    )


@router.post("/workspaces", response_class=HTMLResponse)
def add_workspace(path: str = Form(...)):
    """Register a workspace via the CLI; toast HTML for ``#toast`` (V5)."""
    target = (path or "").strip()
    if not target:
        return HTMLResponse(_toast_html(False, "path is required"), status_code=400)
    ok, message = workspace_service.register_workspace(target)
    return HTMLResponse(_toast_html(ok, message), status_code=200 if ok else 400)


@router.get("/workspaces/fragment", response_class=HTMLResponse)
def workspace_fragment():
    """Workspace cards fragment for 60s HTMX polling (V7)."""
    return HTMLResponse(_fragment_html(_entries()))


@router.get("/workspaces/suggest", response_class=HTMLResponse)
def workspace_suggest():
    """Unregistered chat-session dirs as clickable suggestions (V6)."""
    return HTMLResponse(
        _suggest_html(workspace_service.suggest_workspaces_from_chat_search())
    )

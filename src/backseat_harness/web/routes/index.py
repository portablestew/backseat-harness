"""Homepage routes (step 4.3): workspace list, add, fragment, suggest.

Thin over ``services.workspace``: rendering lives here, registry/chat
seams stay in the service layer. Snippet endpoints return small HTML
fragments for HTMX targets (``#toast``, ``#workspace-list``,
``#ws-suggestions``).
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backseat_harness.web.services import task as task_service
from backseat_harness.web.services import workspace as workspace_service

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

#: The polled workspace list also refreshes immediately when a successful add
#: fires the ``workspaceAdded`` event (``HX-Trigger`` on the POST response).
_POLL_ATTRS = (
    'hx-get="/workspaces/fragment" '
    'hx-trigger="every 60s, workspaceAdded from:body" '
    'hx-swap="outerHTML"'
)


def _entries() -> list[dict[str, object]]:
    """Workspace rows for templates: id + trailing name + full path (V4).

    Also carries the homepage "Tasks" column: the newest task links (by task
    directory mtime) and how many were left out (goal.md:25).
    """
    entries: list[dict[str, object]] = []
    for ws_id, p in workspace_service.list_workspaces_with_ids():
        slugs, more = task_service.list_recent_task_dirs(p)
        entries.append(
            {
                "id": ws_id,
                "name": workspace_service.display_name(p),
                "path": p,
                "tasks_html": _tasks_cell(ws_id, slugs, more),
            }
        )
    return entries


def _tasks_cell(ws_id: object, slugs: list[str], more: int) -> str:
    """The homepage "Tasks" cell: task links plus a trailing "... and N more".

    Built here so the full page (``index.html``) and the polled fragment
    (``_fragment_html``) render byte-identical cells.
    """
    links = ", ".join(
        f'<a class="task-link" href="/ws/{ws_id}/tasks/{html.escape(slug, quote=True)}">'
        f"{html.escape(slug)}</a>"
        for slug in slugs
    )
    if more > 0:
        more_text = f"... and {more} more"
        links = f"{links}, {more_text}" if links else more_text
    return links


def _toast_html(ok: bool, message: str) -> str:
    kind = "ok" if ok else "error"
    return f"<p><strong>{kind}:</strong> {html.escape(message)}</p>"


def _fragment_html(entries: list[dict[str, object]]) -> str:
    if entries:
        rows = "\n".join(
            f'          <tr data-href="/ws/{e["id"]}">'
            f'<td><a href="/ws/{e["id"]}">'
            f"<strong>{html.escape(str(e['name']))}</strong></a></td>"
            f"<td><small>{html.escape(str(e['path']))}</small></td>"
            f'<td>{e["tasks_html"]}</td></tr>'
            for e in entries
        )
        body = (
            '    <table class="data-table">\n'
            "      <thead><tr><th>Workspace</th><th>Path</th><th>Tasks</th>"
            "</tr></thead>\n"
            f"      <tbody>\n{rows}\n      </tbody>\n"
            "    </table>"
        )
    else:
        body = "    <p>No workspaces registered yet.</p>"
    return f"<div id=\"workspace-list\" {_POLL_ATTRS}>\n{body}\n</div>"


def _suggest_html(suggestions: list[str]) -> str:
    """``<option>`` rows for the add-workspace ``<datalist>`` (plan Step C/F2).

    The browser filters these natively as the user types. Each option tags
    whether the dir already has ``.backseat/`` (``ready``) or will be
    initialized when added (``needs init``).
    """
    parts = []
    for s in suggestions:
        status = "ready" if (Path(s) / ".backseat").is_dir() else "needs init"
        value = html.escape(s, quote=True)
        label = html.escape(f"{s} \u00b7 {status}")
        parts.append(f'<option value="{value}">{label}</option>')
    return "\n".join(parts)


@router.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    """Full homepage: workspace cards, add form, suggest button."""
    return templates.TemplateResponse(
        request, "index.html", {"workspaces": _entries(), "suggestions": []}
    )


@router.post("/workspaces", response_class=HTMLResponse)
def add_workspace(path: str = Form(...)):
    """Register a workspace via the CLI; toast HTML for ``#toast`` (V5).

    Plan Step C/F1: an existing plain dir (no ``.backseat/``) is initialized
    with a bare ``.backseat/`` marker before registering, so the dropdown's
    un-initialized suggestions are actually addable. A nonexistent path is
    left to the CLI, which refuses it unchanged (HTTP 400). On success the
    ``HX-Trigger: workspaceAdded`` header refreshes ``#workspace-list`` at once
    instead of waiting for the 60s poll (F8).
    """
    target = (path or "").strip()
    if not target:
        return HTMLResponse(_toast_html(False, "path is required"), status_code=400)
    try:
        initialized = workspace_service.initialize_workspace_marker(target)
    except OSError:
        initialized = False
    ok, message = workspace_service.register_workspace(target)
    if ok and initialized:
        resolved = Path(target).expanduser().resolve()
        message = f"Initialized and registered workspace {resolved}"
    headers = {"HX-Trigger": "workspaceAdded"} if ok else None
    return HTMLResponse(
        _toast_html(ok, message), status_code=200 if ok else 400, headers=headers
    )


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

"""Chat viewer route (step 7.2, 7.5): GET /chat/<provider>/<conversation_id>.

Thin over ``history.load_sessions``: rendering lives here. Unknown provider
or session returns 404.

The URL already names the exact session, so the viewer asks for that key
rather than content-searching for it: one listing plus one parse of the one
conversation, where the search path parses every candidate of that provider
(32 parses against this machine's stores) to find the same row.

The provider parameter is the chat-mother-forker history provider name
(e.g., opencode, kiro_cli, kiro_ide, kiro_ide_v2, claude_code, kilo, cline,
command_code),
which is what load_sessions expects in a key.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backseat_harness import history, manager
from backseat_harness.config import load_config
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import task as task_service

router = APIRouter()

#: Serializes the double-submit check with the staging claim. The route is sync,
#: so two POSTs for one chat can run in parallel threads; without this lock both
#: could pass the check before either creates its staging dir. The lock covers
#: only the claim, not the spawn, so a request never waits on the child.
_GENERATE_CLAIM_LOCK = threading.Lock()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_dt(ts: float) -> str:
    """Format timestamp as YYYY-MM-DD HH:MM."""
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


templates.env.filters["format_datetime"] = _fmt_dt


# Valid chat-mother-forker history provider names (from default_providers())
_VALID_HISTORY_PROVIDERS = {
    "opencode",
    "kiro_cli",
    "kiro_ide",
    "kiro_ide_v2",
    "claude_code",
    "kilo",
    "cline",
    "command_code",
}

# Map chat-mother-forker history provider -> resume_command_template (interactive TUI commands)
# The template receives {conversation_id} which is the native session ID
_RESUME_COMMANDS = {
    "opencode": ["opencode", "--session", "{conversation_id}"],
    "kiro_cli": ["kiro-cli", "chat", "--resume-id", "{conversation_id}"],
    "claude_code": ["claude", "--resume", "{conversation_id}"],
    "command_code": ["cmdc", "--session", "{conversation_id}"],
}


def _build_resume_command(provider: str, conversation_id: str) -> list[str] | None:
    """Build the resume command for a provider and conversation ID."""
    template = _RESUME_COMMANDS.get(provider)
    if not template:
        return None
    return [arg.format(conversation_id=conversation_id) for arg in template]


#: A chat whose store was written this recently is still in progress, so its
def _generate_message(ws_name: str, ws_path: Path) -> str:
    """The confirm dialog text for Generate Task. It names the target workspace."""
    return (
        "Generate a task from this chat?\n\n"
        "Task requirements will be automatically generated from this conversation. "
        "Ensure the chat is finished and the ideas are thoroughly fleshed out "
        "before you proceed.\n\n"
        f"Target workspace: {ws_name}\nPath: {ws_path}"
    )


@dataclass(frozen=True)
class GenerateTaskOffer:
    """A chat's registered workspace, and the Generate Task state for it."""

    provider: str
    conversation_id: str
    ws_id: int
    ws_name: str
    ws_path: Path
    message: str  # the confirm dialog text

    @property
    def conversation_segment(self) -> str:
        """The conversation id as one percent-encoded URL path segment."""
        return quote(self.conversation_id, safe="")


def generate_task_offer(
    provider: str, conversation_id: str, raw_ws: str | None
) -> GenerateTaskOffer | None:
    """The workspace context for a chat reached with ``?ws=<raw_ws>``, or None.

    None when there is no ``ws`` parameter, it is not an integer, or it names no
    registered workspace. Liveness is read only once the workspace resolves.
    """
    if raw_ws is None:
        return None
    try:
        ws_id = int(raw_ws)
    except ValueError:
        return None
    ws_path = task_service.resolve_workspace(ws_id)
    if ws_path is None:
        return None
    return GenerateTaskOffer(
        provider=provider,
        conversation_id=conversation_id,
        ws_id=ws_id,
        ws_name=ws_path.name,
        ws_path=ws_path,
        message=_generate_message(ws_path.name, ws_path),
    )


@router.get("/chat/{provider}/{conversation_id}", response_class=HTMLResponse)
def chat_page(request: Request, provider: str, conversation_id: str):
    """Read-only chat viewer: full transcript for the exact session in the URL."""
    if provider not in _VALID_HISTORY_PROVIDERS:
        return HTMLResponse(f"Unknown provider: {provider}", status_code=404)

    # Exact key (provider:conversation_id) -- see the module docstring for why
    # this is not a content search.
    search_key = f"{provider}:{conversation_id}"

    try:
        transcript = history.load_sessions([search_key]).get(search_key)
    except history.HistoryError as e:
        return HTMLResponse(str(e), status_code=404)

    if not transcript:
        return HTMLResponse("Session not found or transcript empty", status_code=404)

    # Check if this provider supports resume
    resume_command = _build_resume_command(provider, conversation_id)

    # Goal G6: the sticky header shows the same normalized 120-char prompt
    # string the workspace lists show -- reuse the 3e helper, no duplication.
    prompt = chat_search_service._prompt_excerpt(transcript)

    # A managed/operator session is a task's own transcript, so it carries no
    # chat affordances: no Generate Task (it already is one), no Resume, no
    # Fork. It also may still be running -- the task page links here to follow
    # it live -- so it reloads itself on the task page's own 60s cadence. A
    # plain chat is not a task, and stays static.
    is_task_session = chat_search_service.classify_session(transcript) != "other"

    # Back-nav (3c pattern) to the workspace the chat list lives on, when the
    # referral carries ``?ws=<id>``; without it, fall back to the homepage.
    # The same workspace context also drives Generate Task (see the helper).
    generate_task = generate_task_offer(provider, conversation_id, request.query_params.get("ws"))
    ws_id = generate_task.ws_id if generate_task is not None else None
    ws_name = generate_task.ws_name if generate_task is not None else None

    # Goal G7: the continue button launches the web shell in the chat's own
    # workspace when it is known (fixing the cwd='.' default: the *server's*
    # cwd, not the chat's).
    chat_cwd = str(generate_task.ws_path) if generate_task is not None else "."

    # This page's own back link: an explicit ``from`` (the task page that linked
    # here) wins, then the referral's workspace, then the homepage. Only a
    # same-site path is honored -- an off-site ``from`` would be an open
    # redirect.
    from_param = request.query_params.get("from")
    if from_param and from_param.startswith("/") and not from_param.startswith("//"):
        back_href = from_param
        back_label = request.query_params.get("label") or "Back"
    elif ws_name:
        back_href, back_label = f"/ws/{ws_id}", ws_name
    else:
        back_href, back_label = "/", "Workspaces"

    # The shell page's back link is this page, reached through ``?from=``, so
    # the payload must re-carry ``?ws=``; without it the round trip lands on
    # the bare transcript and the header degrades to the "Workspaces" homepage.
    back_url = f"/chat/{provider}/{quote(conversation_id, safe='')}"
    if ws_id is not None:
        back_url = f"{back_url}?ws={ws_id}"

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "provider": provider,
            "conversation_id": conversation_id,
            "back_url": back_url,
            "back_href": back_href,
            "back_label": back_label,
            "current_session_key": search_key,
            "transcript": transcript,
            "resume_command": resume_command,
            "prompt": prompt,
            "ws_id": ws_id,
            "ws_name": ws_name,
            "chat_cwd": chat_cwd,
            "generate_task": generate_task,
            "auto_refresh": is_task_session,
            "is_task_session": is_task_session,
        },
    )


@router.post("/chat/{provider}/{conversation_id}/generate-task")
def generate_task(
    provider: str,
    conversation_id: str,
    ws: int = Form(...),
    confirmed: str = Form(""),
):
    """Start a background generate-task run for this chat, then go to the workspace.

    Refusals, in order: 404 for an unknown provider, an unregistered workspace,
    or a missing transcript; 400 when the confirm dialog did not say yes; 409
    when a generation for it is already running. The run is
    the ``generate-task`` CLI, detached, so this request never waits on it.
    """
    if provider not in _VALID_HISTORY_PROVIDERS:
        return HTMLResponse(f"Unknown provider: {provider}", status_code=404)
    ws_path = task_service.resolve_workspace(ws)
    if ws_path is None:
        return HTMLResponse(f"Unknown workspace: {ws}", status_code=404)
    if confirmed != "yes":
        return HTMLResponse("Generate Task needs confirmation", status_code=400)

    key = f"{provider}:{conversation_id}"
    try:
        transcript = history.load_sessions([key]).get(key)
    except history.HistoryError as e:
        return HTMLResponse(str(e), status_code=404)
    if not transcript:
        return HTMLResponse("Session not found or transcript empty", status_code=404)

    config = load_config(ws_path)
    with _GENERATE_CLAIM_LOCK:
        if manager.active_generation(config.tasks_dir, key):
            return HTMLResponse(
                "A task is already being generated from this chat.", status_code=409
            )
        staging = manager.new_staging(config.tasks_dir, key, str(ws_path))
    task_service.generate_task_detached(ws_path, key, staging.name)
    return RedirectResponse(url=f"/ws/{ws}", status_code=303)

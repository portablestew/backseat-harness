"""Chat viewer route (step 7.2, 7.5): GET /chat/<provider>/<conversation_id>.

Thin over ``history.load_sessions``: rendering lives here. Unknown provider
or session returns 404.

The URL already names the exact session, so the viewer asks for that key
rather than content-searching for it: one listing plus one parse of the one
conversation, where the search path parses every candidate of that provider
(32 parses against this machine's stores) to find the same row.

The provider parameter is the chat-mother-forker history provider name
(e.g., opencode, kiro_cli, kiro_ide, kiro_ide_v2, claude_code, kilo, cline),
which is what load_sessions expects in a key.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backseat_harness import history
from backseat_harness.web.services import chat_search as chat_search_service
from backseat_harness.web.services import task as task_service

router = APIRouter()

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
}

# Map chat-mother-forker history provider -> resume_command_template (interactive TUI commands)
# The template receives {conversation_id} which is the native session ID
_RESUME_COMMANDS = {
    "opencode": ["opencode", "--session", "{conversation_id}"],
    "kiro_cli": ["kiro-cli", "chat", "--resume-id", "{conversation_id}"],
    "claude_code": ["claude", "--resume", "{conversation_id}"],
}


def _build_resume_command(provider: str, conversation_id: str) -> list[str] | None:
    """Build the resume command for a provider and conversation ID."""
    template = _RESUME_COMMANDS.get(provider)
    if not template:
        return None
    return [arg.format(conversation_id=conversation_id) for arg in template]


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

    # Back-nav (3c pattern) to the workspace the chat list lives on, when the
    # referral carries ``?ws=<id>``; without it, fall back to the homepage.
    ws_id: int | None = None
    ws_name: str | None = None
    ws_path_resolved: Path | None = None
    raw_ws = request.query_params.get("ws")
    if raw_ws is not None:
        try:
            ws_path_resolved = task_service.resolve_workspace(int(raw_ws))
        except ValueError:
            ws_path_resolved = None
        if ws_path_resolved is not None:
            ws_id = int(raw_ws)
            ws_name = ws_path_resolved.name

    # Goal G7: the continue button launches the web shell in the chat's own
    # workspace when it is known (fixing the cwd='.' default: the *server's*
    # cwd, not the chat's).
    chat_cwd = str(ws_path_resolved) if ws_path_resolved is not None else "."

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "provider": provider,
            "conversation_id": conversation_id,
            "transcript": transcript,
            "resume_command": resume_command,
            "prompt": prompt,
            "ws_id": ws_id,
            "ws_name": ws_name,
            "chat_cwd": chat_cwd,
        },
    )
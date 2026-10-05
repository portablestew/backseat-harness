"""Chat viewer route (step 7.2, 7.5): GET /chat/<provider>/<conversation_id>.

Thin over ``history.load_full_transcript``: rendering lives here.
Unknown provider or session returns 404.

The provider parameter is the chat-mother-forker history provider name
(e.g., opencode, kiro_cli, kiro_ide, kiro_ide_v2, claude_code, kilo, cline),
which is what load_full_transcript expects.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from backseat_harness import history

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
    """Read-only chat viewer: full transcript via history.load_full_transcript."""
    if provider not in _VALID_HISTORY_PROVIDERS:
        return HTMLResponse(f"Unknown provider: {provider}", status_code=404)

    # Build search key for chat-mother-forker (format: "provider:conversation_id")
    search_key = f"{provider}:{conversation_id}"

    try:
        transcript = history.load_full_transcript(search_key, provider)
    except history.HistoryError as e:
        return HTMLResponse(str(e), status_code=404)

    if not transcript:
        return HTMLResponse("Session not found or transcript empty", status_code=404)

    # Check if this provider supports resume
    resume_command = _build_resume_command(provider, conversation_id)

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "provider": provider,
            "conversation_id": conversation_id,
            "transcript": transcript,
            "resume_command": resume_command,
        },
    )
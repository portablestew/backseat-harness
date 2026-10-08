"""Raw session search routes (step 6.2, 6.4): GET /search?q=..., POST /search/fork.

GET /search is thin over ``services.chat_search.search_all_sessions``. The
fork POST only needs session *keys* to pick a provider, so it lists them
through ``history.list_session_refs`` -- the cheap, unparsed store scan --
instead of loading 200 transcripts it would never read.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import anyio
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from backseat_harness import history
from backseat_harness.web.services import chat_search as chat_search_service

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _fmt_dt(ts: float) -> str:
    """Format timestamp as YYYY-MM-DD HH:MM."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# Custom filter: timestamp -> human-readable
templates.env.filters["format_datetime"] = _fmt_dt


# Map chat-mother-forker history provider -> resume_command_template (interactive TUI commands)
# The template receives {conversation_id} which is the native session ID
_RESUME_COMMANDS = {
    "opencode": ["opencode", "--session", "{conversation_id}"],
    "kiro_cli": ["kiro-cli", "chat", "--resume-id", "{conversation_id}"],
    "claude_code": ["claude", "--resume", "{conversation_id}"],
}

# Supported providers (keys match chat-mother-forker history provider names)
_SUPPORTED_RESUME_PROVIDERS = {"opencode", "kiro_cli", "claude_code"}


def _extract_provider_from_key(key: str) -> str | None:
    """Extract provider name from a ChatRef key (e.g., 'opencode:ses_...' -> 'opencode')."""
    if not key or ":" not in key:
        return None
    return key.split(":", 1)[0]


def _build_resume_command(provider: str, conversation_id: str) -> list[str] | None:
    """Build the resume command for a provider and conversation ID."""
    template = _RESUME_COMMANDS.get(provider)
    if not template:
        return None
    return [arg.format(conversation_id=conversation_id) for arg in template]


def _build_fork_command(fork_provider: str, conversation_id: str) -> list[str]:
    """Build the fork command for a provider.
    
    Fork opens a fresh interactive TUI session (no session ID). The initial
    prompt "Please fork chat id <conversation_id>" is delivered via PTY write
    after the session starts (see search_fork).
    """
    if fork_provider == "opencode":
        return ["opencode"]
    elif fork_provider == "kiro":
        return ["kiro-cli", "chat"]
    elif fork_provider == "claude":
        return ["claude"]
    else:
        # Fallback - shouldn't happen as pick_fork_provider only returns supported
        raise ValueError(f"Unknown fork provider: {fork_provider}")


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = Query(default="")):
    """Session search page: provider, preview, mtime, project, classification."""
    # Limit to 20 for reasonable response time; full 200 is too slow for web UI
    results = await anyio.to_thread.run_sync(chat_search_service.search_all_sessions, q, 20)

    # Enrich results with resume/fork info
    enriched_results = []
    for r in results:
        provider = _extract_provider_from_key(r.key)
        conversation_id = r.native_id or r.key.partition(":")[2]
        
        resume_command = None
        fork_provider = None
        fork_command = None
        
        if provider in _SUPPORTED_RESUME_PROVIDERS:
            resume_command = _build_resume_command(provider, conversation_id)
        else:
            # Non-supported provider: determine fork provider
            try:
                fork_provider = chat_search_service.pick_fork_provider(r.key, results)
                fork_command = _build_fork_command(fork_provider, conversation_id)
            except RuntimeError:
                # No supported provider available
                pass
        
        enriched_results.append({
            "key": r.key,
            "native_id": r.native_id,
            "project": r.project,
            "mtime": r.mtime,
            "preview": r.preview,
            "classification": r.classification,
            "provider": provider,
            "conversation_id": conversation_id,
            "resume_command": resume_command,
            "fork_provider": fork_provider,
            "fork_command": fork_command,
        })

    template = templates.get_template("search.html")
    html = template.render({"request": request, "query": q, "results": enriched_results})
    return HTMLResponse(html)


@router.post("/search/fork", response_class=HTMLResponse)
async def search_fork(
    request: Request,
    conversation_id: str = Form(...),
    current_session_key: str = Form(...),
):
    """Fork a session using the selected provider, start a shell session, and redirect."""
    # Provider selection reads keys only (pick_fork_provider rule 2), so list
    # them from the cheap store scan: no transcript is parsed here.
    refs = await anyio.to_thread.run_sync(history.list_session_refs, 200)

    # Determine fork provider
    fork_provider = chat_search_service.pick_fork_provider(current_session_key, refs)
    
    # Build fork command (fresh TUI, no session ID)
    fork_command = _build_fork_command(fork_provider, conversation_id)
    
    # Start shell session
    from backseat_harness.web.routes.shell import get_manager
    manager = get_manager()
    session_id = manager.create_session(fork_command, Path("."))
    
    # Send initial fork prompt to the PTY after a brief delay for TUI startup
    import asyncio
    await asyncio.sleep(0.5)  # let the TUI initialize
    fork_prompt = f"Please fork chat id {conversation_id}\n"
    manager.write(session_id, fork_prompt.encode())
    
    # Redirect to shell page
    return RedirectResponse(url=f"/shell/{session_id}", status_code=303)
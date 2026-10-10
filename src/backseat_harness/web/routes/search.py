"""Raw session search routes (step 6.2, 6.4): GET /search/results (homepage
fragment), POST /search/fork.

The results fragment is thin over
``services.chat_search.homepage_search``. The fork POST only needs session
*keys* to pick a provider, so it lists them through
``history.list_session_refs`` -- the cheap, unparsed store scan -- instead of
loading 200 transcripts it would never read.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

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


def _extract_provider_from_key(key: str) -> str | None:
    """Extract provider name from a ChatRef key (e.g., 'opencode:ses_...' -> 'opencode')."""
    if not key or ":" not in key:
        return None
    return key.split(":", 1)[0]


def _fork_prompt(source_provider: str, conversation_id: str) -> str:
    """The initial prompt handed to the forked CLI at launch.

    Names the *source* provider and session so the target CLI's chat tools can
    find the conversation to fork, e.g. ``Please fork kilo chat session XXXXX``.
    """
    return f"Please fork {source_provider} chat session {conversation_id}"


def _build_fork_command(fork_provider: str, prompt: str) -> list[str]:
    """Build the fork launch command, with the initial prompt in argv.

    The prompt must be an argument at launch, not a delayed PTY write: each
    fork CLI accepts an initial prompt on the command line (kiro-cli's
    positional ``[INPUT]``, opencode's ``--prompt``, claude's positional
    prompt). Writing it after a fixed delay raced the TUI startup, so kiro-cli
    opened without the prompt.
    """
    if fork_provider == "opencode":
        return ["opencode", "--prompt", prompt]
    elif fork_provider == "kiro":
        return ["kiro-cli", "chat", prompt]
    elif fork_provider == "claude":
        return ["claude", prompt]
    else:
        # Fallback - shouldn't happen as pick_fork_provider only returns supported
        raise ValueError(f"Unknown fork provider: {fork_provider}")


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(request: Request, q: str = Query(default="")):
    """Homepage search results fragment (HTMX): task chats (deduped, enriched)
    interleaved with other chats, in chat-search order."""
    rows = await anyio.to_thread.run_sync(chat_search_service.homepage_search, q)
    return templates.TemplateResponse(
        request, "search_results.html", {"rows": rows, "query": q}
    )


@router.post("/search/fork", response_class=HTMLResponse)
async def search_fork(
    request: Request,
    conversation_id: str = Form(...),
    current_session_key: str = Form(...),
    back_url: str = Form("/"),
    back_label: str = Form(""),
    title: str = Form(""),
):
    """Fork a session using the selected provider, start a shell session, and redirect."""
    # Provider selection reads keys only (pick_fork_provider rule 2), so list
    # them from the cheap store scan: no transcript is parsed here.
    refs = await anyio.to_thread.run_sync(history.list_session_refs, 200)

    # Determine fork provider
    fork_provider = chat_search_service.pick_fork_provider(current_session_key, refs)

    # The initial prompt is the launch command's last argument (no delayed PTY
    # write): the source provider names the chat the forked CLI should fork.
    source_provider = _extract_provider_from_key(current_session_key) or "chat"
    fork_command = _build_fork_command(
        fork_provider, _fork_prompt(source_provider, conversation_id)
    )

    # Start shell session
    from backseat_harness.web.routes.shell import get_manager
    manager = get_manager()
    session_id = manager.create_session(fork_command, Path("."))

    # Redirect to shell page, carrying the origin so the shell header's back
    # link returns to the chat page.
    params = {"from": back_url or "/", "label": back_label or "Chat"}
    if title:
        params["title"] = title
    return RedirectResponse(
        url=f"/shell/{session_id}?{urlencode(params)}",
        status_code=303,
    )

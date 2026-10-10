"""Shell routes (step 8.3-8.4): POST /shell/start, WebSocket /ws/shell/<session_id>,
GET /shell/<session_id>."""

from __future__ import annotations

import asyncio
import base64
import json
import queue
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from backseat_harness.web.shell.manager import PTYSessionManager

router = APIRouter()

# Module-level singleton manager (web-app-owned state)
_manager: PTYSessionManager | None = None


def get_manager() -> PTYSessionManager:
    """Get or create the singleton PTY session manager."""
    global _manager
    if _manager is None:
        _manager = PTYSessionManager()
    return _manager


class ShellStartRequest(BaseModel):
    """Request body for starting a shell session.

    ``cols/rows`` (plan 3g) optionally set the PTY's initial size at spawn.
    """

    cmd: list[str]
    cwd: str
    cols: int | None = None
    rows: int | None = None


class ShellStartResponse(BaseModel):
    """Response body for starting a shell session."""
    session_id: str


@router.post("/shell/start", response_model=ShellStartResponse)
def shell_start(req: ShellStartRequest):
    """Start a new PTY shell session and return its ID."""
    manager = get_manager()
    session_id = manager.create_session(req.cmd, Path(req.cwd), cols=req.cols, rows=req.rows)
    return ShellStartResponse(session_id=session_id)


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _is_safe_relative(url: str) -> bool:
    """Same-origin relative-URL check for back-navigation targets."""
    return url.startswith("/") and not url.startswith("//")


def _label_for_path(path: str) -> str:
    if path == "/":
        return "Workspaces"
    if path.startswith("/ws/"):
        return "Workspace"
    if path.startswith("/chat/"):
        return "Chat"
    return "Back"


#: Longest header title the shell page renders; openers already bound their
#: own titles (a prompt excerpt is at most 123 characters), so this only trims
#: a hand-written ``?title=`` URL.
_TITLE_MAX = 160


def _shell_title(request: Request, fallback: str) -> str:
    """The shell header's top line: the opener's ``?title=`` (task name, prompt
    excerpt, or "New <ws> <provider>"), whitespace-collapsed and length-bounded.
    The command is shown only when no title is known.
    """
    raw = " ".join(request.query_params.get("title", "").split())
    if not raw:
        return fallback
    if len(raw) > _TITLE_MAX:
        return raw[: _TITLE_MAX - 3] + "..."
    return raw


def _resolve_back(request: Request) -> tuple[str, str]:
    """Resolve (url, label) for the shell page's back link.

    Prefers the opener-supplied ``?from=&label=`` query params, falls back
    to a same-origin Referer path, and finally to the workspaces index.
    """
    raw = request.query_params.get("from", "")
    if _is_safe_relative(raw):
        label = request.query_params.get("label", "")
        return raw, (label or _label_for_path(raw))

    referer = request.headers.get("referer", "")
    parts = urlsplit(referer)
    if (
        parts.scheme in ("http", "https")
        and parts.netloc == request.url.netloc
        and parts.path.startswith("/")
    ):
        # Keep the query string: it can carry the context (``?ws=``) the
        # opener page needs to render its own back link.
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        return target, _label_for_path(parts.path)

    return "/", "Workspaces"


@router.get("/shell/{session_id}", response_class=HTMLResponse)
def shell_page(request: Request, session_id: str):
    """Serve the xterm shell page for an existing session.

    The page connects to the WebSocket at /ws/shell/{session_id}.
    """
    manager = get_manager()
    session = manager.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    back_url, back_label = _resolve_back(request)
    return templates.TemplateResponse(
        request,
        "shell.html",
        {
            "session_id": session_id,
            "ws_path": f"/ws/shell/{session_id}",
            "title": _shell_title(request, " ".join(session.cmd)),
            "back_url": back_url,
            "back_label": back_label,
        },
    )


@router.post("/shell/{session_id}/stop")
def shell_stop(session_id: str, back_url: str = Form("/")):
    """Kill the session's PTY and redirect back to the origin page."""
    manager = get_manager()
    manager.kill_session(session_id)
    target = back_url if _is_safe_relative(back_url) else "/"
    return RedirectResponse(url=target, status_code=303)


@router.websocket("/ws/shell/{session_id}")
async def shell_ws(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for PTY shell I/O.

    Protocol:
    - Server -> Client: binary frames with raw PTY output bytes
    - Client -> Server: text frames with JSON commands:
        {"type": "input", "data": "base64-encoded-bytes"}  -- send input to PTY
        {"type": "resize", "cols": 80, "rows": 24}        -- resize PTY
        {"type": "ping"}                                  -- keepalive
    - On connect: subscriber attached, receives ring-buffer replay
    - On disconnect: subscriber detached
    """
    manager = get_manager()

    # Bridge: sync subscriber puts data in queue, async loop reads from queue
    output_queue: queue.Queue[bytes] = queue.Queue()

    def sync_send_output(data: bytes):
        output_queue.put(data)

    attached = manager.attach(session_id, sync_send_output)
    if not attached:
        await websocket.close(code=4004, reason="Session not found")
        return

    await websocket.accept()

    # Task to forward queued output to WebSocket
    async def forward_output():
        while True:
            try:
                data = output_queue.get_nowait()
                await websocket.send_bytes(data)
            except queue.Empty:
                await asyncio.sleep(0.01)
            except Exception:
                break

    forward_task = asyncio.create_task(forward_output())

    try:
        while True:
            # Receive JSON command from client
            msg = await websocket.receive_text()
            try:
                cmd = json.loads(msg)
            except json.JSONDecodeError:
                continue

            cmd_type = cmd.get("type")
            if cmd_type == "input":
                # Send input to PTY (base64 encoded)
                data_b64 = cmd.get("data", "")
                try:
                    data = base64.b64decode(data_b64)
                    manager.write(session_id, data)
                except Exception:
                    pass
            elif cmd_type == "resize":
                cols = cmd.get("cols", 80)
                rows = cmd.get("rows", 24)
                manager.resize(session_id, cols, rows)
            elif cmd_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        forward_task.cancel()
        try:
            await asyncio.wait_for(forward_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass
        manager.detach(session_id, sync_send_output)
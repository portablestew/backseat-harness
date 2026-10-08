"""Shell routes (step 8.3-8.4): POST /shell/start, WebSocket /ws/shell/<session_id>,
GET /shell/<session_id>."""

from __future__ import annotations

import asyncio
import base64
import json
import queue
from pathlib import Path

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
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


@router.get("/shell/{session_id}", response_class=HTMLResponse)
def shell_page(request: Request, session_id: str):
    """Serve the xterm shell page for an existing session.

    The page connects to the WebSocket at /ws/shell/{session_id}.
    """
    manager = get_manager()
    if not manager.get_session(session_id):
        return HTMLResponse("Session not found", status_code=404)

    return templates.TemplateResponse(
        request,
        "shell.html",
        {
            "session_id": session_id,
            "ws_path": f"/ws/shell/{session_id}",
        },
    )


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
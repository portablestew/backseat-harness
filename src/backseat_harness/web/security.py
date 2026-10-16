"""Host/Origin allowlist middleware (scaffold step 3.2, websocket fix step 9.4a).

One pure-ASGI enforcement point for *both* scope types. The previous
``BaseHTTPMiddleware`` implementation only ran for ``scope["type"] == "http"``
and passed WebSocket scopes straight through to the app, so
``/ws/shell/<id>`` accepted handshakes carrying a foreign Host or Origin —
cross-site WebSocket hijacking of a PTY shell (goal.md:3).
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.types import ASGIApp, Receive, Scope, Send

_FORBIDDEN_HOST = b'{"detail": "forbidden host"}'
_FORBIDDEN_ORIGIN = b'{"detail": "forbidden origin"}'
_JSON_HEADERS = [(b"content-type", b"application/json")]


def _header(scope: Scope, name: bytes) -> str:
    """Value of a request header, lowercased/trimmed (empty when absent)."""
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1").strip().lower()
    return ""


class HostOriginAllowlistMiddleware:
    """Reject non-localhost Host/Origin with 403 (DNS rebinding defense).

    Strict allowlist: ``127.0.0.1:<port>`` + ``localhost:<port>`` only, plus
    any ``extra_hosts`` (the Caddy site addresses in caddy mode). Both headers
    are checked when present, on HTTP requests and WebSocket handshakes alike —
    a WebSocket is refused before ``websocket.accept``.
    """

    def __init__(self, app: ASGIApp, port: int = 8090, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.port = port
        self.allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed.update(host.lower() for host in extra_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        host = _header(scope, b"host")
        if host not in self.allowed:
            await self._reject(scope, send, _FORBIDDEN_HOST)
            return

        origin = _header(scope, b"origin")
        if origin:
            netloc = urlparse(origin).netloc.lower()
            if netloc not in self.allowed:
                await self._reject(scope, send, _FORBIDDEN_ORIGIN)
                return

        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, send: Send, body: bytes) -> None:
        """Send 403 for an HTTP request, or refuse the WebSocket handshake."""
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": _JSON_HEADERS
                    + [(b"content-length", str(len(body)).encode("ascii"))],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        # WebSocket: deny before websocket.accept. Servers advertise the
        # websocket.http.response extension for a full HTTP error response;
        # without it, close-before-accept is the spec's reject signal.
        if "websocket.http.response" in scope.get("extensions", {}):
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 403,
                    "headers": _JSON_HEADERS,
                }
            )
            await send({"type": "websocket.http.response.body", "body": body})
        else:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": body.decode("latin-1"),
                }
            )

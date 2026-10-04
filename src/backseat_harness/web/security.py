"""Host/Origin allowlist middleware (scaffold step 3.2)."""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse


class HostOriginAllowlistMiddleware(BaseHTTPMiddleware):
    """Reject non-localhost Host/Origin with 403 (DNS rebinding defense).

    Strict allowlist: ``127.0.0.1:<port>`` + ``localhost:<port>`` only.
    Both headers are checked when present.
    """

    def __init__(self, app, port: int = 8080) -> None:
        super().__init__(app)
        self.port = port
        self.allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        host = (request.headers.get("host") or "").strip().lower()
        if host not in self.allowed:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            netloc = urlparse(origin).netloc.lower()
            if netloc not in self.allowed:
                return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        return await call_next(request)

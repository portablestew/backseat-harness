"""Web app factory (steps 3-5): localhost allowlist + homepage + workspace routes."""

from __future__ import annotations

from fastapi import FastAPI

from backseat_harness.web.routes import index as index_routes
from backseat_harness.web.routes import workspace as workspace_routes
from backseat_harness.web.security import HostOriginAllowlistMiddleware


def create_app(port: int = 8080) -> FastAPI:
    """Create the web FastAPI app with localhost-only allowlist."""
    app = FastAPI(title="backseat-harness web")
    app.add_middleware(HostOriginAllowlistMiddleware, port=port)
    app.include_router(index_routes.router)
    app.include_router(workspace_routes.router)
    return app

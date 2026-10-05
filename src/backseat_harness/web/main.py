"""Web app factory (steps 3-7): localhost allowlist + homepage, workspace, search,
task, chat routes."""

from __future__ import annotations

from fastapi import FastAPI

from backseat_harness.web.routes import chat as chat_routes
from backseat_harness.web.routes import index as index_routes
from backseat_harness.web.routes import search as search_routes
from backseat_harness.web.routes import task as task_routes
from backseat_harness.web.routes import workspace as workspace_routes
from backseat_harness.web.security import HostOriginAllowlistMiddleware


def create_app(port: int = 8080) -> FastAPI:
    """Create the web FastAPI app with localhost-only allowlist."""
    app = FastAPI(title="backseat-harness web")
    app.add_middleware(HostOriginAllowlistMiddleware, port=port)
    app.include_router(index_routes.router)
    app.include_router(workspace_routes.router)
    app.include_router(search_routes.router)
    app.include_router(task_routes.router)
    app.include_router(chat_routes.router)
    return app

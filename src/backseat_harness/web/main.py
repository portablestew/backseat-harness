"""Web app factory (steps 3-8): localhost allowlist + homepage, workspace, search,
task, chat, shell routes."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI

from backseat_harness.web.routes import chat as chat_routes
from backseat_harness.web.routes import index as index_routes
from backseat_harness.web.routes import search as search_routes
from backseat_harness.web.routes import shell as shell_routes
from backseat_harness.web.routes import task as task_routes
from backseat_harness.web.routes import workspace as workspace_routes
from backseat_harness.web.security import HostOriginAllowlistMiddleware


def create_app(port: int = 8090, extra_hosts: Iterable[str] = ()) -> FastAPI:
    """Create the web FastAPI app with localhost-only allowlist.

    ``extra_hosts`` adds Host/Origin strings beyond localhost (caddy mode's
    site addresses); empty keeps the localhost-only allowlist.
    """
    app = FastAPI(title="backseat-harness web")
    app.add_middleware(HostOriginAllowlistMiddleware, port=port, extra_hosts=tuple(extra_hosts))
    app.include_router(index_routes.router)
    app.include_router(workspace_routes.router)
    app.include_router(search_routes.router)
    app.include_router(task_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(shell_routes.router)
    return app

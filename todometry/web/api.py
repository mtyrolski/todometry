"""Composition root for the local Todometry API."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from todometry.core.utils import configure_runtime_logging
from todometry.core.version import get_version
from todometry.dashboard.settings import resolve_dashboard_config_path
from todometry.web.api_components.dashboard_runtime import DashboardState
from todometry.web.routes import admin_settings, dashboard, runtime_admin


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    dashboard_state = DashboardState()
    app.state.dashboard = dashboard_state
    app.state.dashboard_config_path = resolve_dashboard_config_path()
    try:
        yield
    finally:
        try:
            # The job may still be writing local files through asyncio.to_thread.
            task = dashboard_state.sync.task
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        finally:
            del app.state.dashboard
            del app.state.dashboard_config_path


def create_app() -> FastAPI:
    app = FastAPI(title="todometry API", version=get_version(), lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(dashboard.router)
    app.include_router(runtime_admin.router)
    app.include_router(admin_settings.router)

    @app.get("/api/health", tags=["health"])
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok", "version": get_version()}

    return app


configure_runtime_logging()
app = create_app()

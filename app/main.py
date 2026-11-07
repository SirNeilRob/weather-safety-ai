"""FastAPI application factory.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

The ``app`` attribute is created lazily so importing this module in tests does
not touch the filesystem.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .config import Settings
from .context import AppContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "Front End"

DESCRIPTION = """
Offline-first backend for the SharkByte hackathon app.

* Submits work to the Swarm API when the device has connectivity.
* Falls back to a local processor when it does not, and keeps the job.
* Reconciles locally-produced results upstream when the link returns.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    context = AppContext(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await context.start()
        try:
            yield
        finally:
            await context.stop()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.ctx = context

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # hackathon default; tighten for anything public
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/v1", tags=["meta"])
    async def index() -> dict:
        return {
            "service": settings.app_name,
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/v1/health",
            "offline_capable": True,
            "endpoints": [
                "POST /v1/jobs",
                "GET /v1/jobs/{id}",
                "GET /v1/jobs",
                "GET /v1/health",
                "GET /v1/capabilities",
                "POST /v1/demo/offline",
                "POST /v1/sync/run",
                "GET /v1/alerts/active",
            ],
        }

    # The dashboard is served by the backend itself, so the phone loads the page
    # and the API from one address and nothing depends on a second server.
    frontend = Path(os.getenv("FRONTEND_DIR", FRONTEND_DIR))
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="dashboard")

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazy module attribute so ``uvicorn app.main:app`` works without import side effects."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

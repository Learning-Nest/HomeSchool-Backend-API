"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import dispose_engine
from app.errors import install_error_handlers
from app.logging_setup import setup_logging
from app.routers import admin, auth, children, curriculum, families, health, me, planned, plans, progress, scrum, sessions

log = logging.getLogger("app.access")
API_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.seed_on_startup and s.seed_bundle_path:
        from app.cli import seed_from_path

        seed_from_path(s.seed_bundle_path, auto_publish=True)
    log.info("started", extra={"env": s.app_env, "version": API_VERSION})
    yield
    dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    app = FastAPI(
        title="HomeSchooling API",
        version=API_VERSION,
        lifespan=lifespan,
        docs_url=None if s.app_env == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if s.app_env == "prod" else "/openapi.json",
    )
    install_error_handlers(app)

    if s.trusted_hosts != "*":
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=[h.strip() for h in s.trusted_hosts.split(",") if h.strip()]
        )
    if s.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origin_list,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-Elevation-Token", "X-App-Version"],
            expose_headers=["X-Request-Id"],
            max_age=600,
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = rid[:64]
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        log.info(
            "request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "route": getattr(route, "path", request.url.path),
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    # The Scrum board is a separate internal tool with its own per-person access code (app/routers/scrum.py),
    # meant to be called from a static page hosted wherever the team puts it -- an origin the main CORS allowlist
    # above doesn't know about. Open CORS for just that prefix rather than loosening the app-wide policy.
    @app.middleware("http")
    async def scrum_cors(request: Request, call_next):
        if not request.url.path.startswith("/v1/scrum"):
            return await call_next(request)
        if request.method == "OPTIONS":
            response = Response(status_code=204)
        else:
            response = await call_next(request)
        origin = request.headers.get("Origin")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Scrum-Code"
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    for module in (health, auth, me, families, children, curriculum, plans, sessions, progress, admin, planned, scrum):
        app.include_router(module.router)
    return app

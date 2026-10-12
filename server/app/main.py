from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from .config import get_settings
from .db import init_db
from .routes.auth import router as auth_router
from .routes.email_prefs import router as email_prefs_router
from .routes.net import router as net_router

logging.basicConfig(level=logging.INFO)

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()  # for production schema changes, introduce Alembic migrations
    yield


def create_app() -> FastAPI:
    s = get_settings()
    docs = s.enable_docs and s.environment != "production"
    app = FastAPI(
        title="DedSec CCTV Auth",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 16_384:
            return Response(status_code=413, content="Request too large")
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if "server" in response.headers:
            del response.headers["server"]
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=s.allowed_hosts)
    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origins,  # explicit list only, never "*"
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )
    if s.force_https:
        app.add_middleware(HTTPSRedirectMiddleware)

    app.include_router(auth_router)
    app.include_router(email_prefs_router)
    app.include_router(net_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

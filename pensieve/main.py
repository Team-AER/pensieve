"""FastAPI application factory. Routers are owned by their packages; register nothing else here."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from pensieve.config import get_settings
from pensieve.db import dispose_engine, get_engine

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Pensieve", version="0.1.0", lifespan=lifespan, debug=settings.debug)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Package routers. Each package exposes `router` from its __init__; keep this list stable.
    from pensieve.syncapi import router as syncapi_router
    from pensieve.web import router as web_router

    app.include_router(syncapi_router)
    app.include_router(web_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    @app.exception_handler(StarletteHTTPException)
    async def _auth_redirect(request: Request, exc: StarletteHTTPException):
        # Browser requests without a session go to the login page; API clients get JSON.
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and wants_html and not request.url.path.startswith("/api"):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app


app = create_app()

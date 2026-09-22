"""FastAPI application factory. Routers are owned by their packages; register nothing else here."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from pensieve.config import get_settings
from pensieve.db import dispose_engine, get_engine

STATIC_DIR = Path(__file__).parent / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src * data:; "
    "media-src *; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "script-src 'self' 'unsafe-inline'; "
    "frame-src 'self' https:; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """CSP, nosniff, referrer policy and frame denial on every response; HSTS only behind TLS."""

    def __init__(self, app, *, hsts: bool) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        if self.hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Pensieve", version="0.1.0", lifespan=lifespan, debug=settings.debug)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.session_cookie_secure)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Package routers. Each package exposes `router` from its __init__; keep this list stable.
    from pensieve.syncapi import router as syncapi_router
    from pensieve.web import router as web_router

    app.include_router(syncapi_router)
    app.include_router(web_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    def _html_client(request: Request) -> bool:
        """A browser navigation (not the sync APIs, not an HTMX partial request)."""
        path = request.url.path
        if path.startswith(("/api", "/reader/api")) or request.headers.get("hx-request") == "true":
            return False
        return "text/html" in request.headers.get("accept", "")

    @app.exception_handler(StarletteHTTPException)
    async def _auth_redirect(request: Request, exc: StarletteHTTPException):
        # Browser requests without a session go to the login page; API clients get JSON.
        if _html_client(request):
            if exc.status_code == 401:
                target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(url=f"/login?next={quote(target, safe='/')}", status_code=303)
            if exc.status_code in (403, 404, 500):
                return _error_page(request, exc.status_code, exc.headers)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception):
        if _html_client(request):
            return _error_page(request, 500)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    def _error_page(request: Request, status_code: int, headers: dict | None = None) -> Response:
        # Rendered by the web package's Jinja environment; anonymous so a broken session can't break the page.
        from pensieve.web.templating import render

        return render(
            request, "error.html", {"status_code": status_code}, status_code=status_code, headers=headers
        )

    return app


app = create_app()

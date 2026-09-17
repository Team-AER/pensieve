"""Registers every web sub-router on the package `router` (imported by pensieve.main)."""

from pathlib import Path

from fastapi.responses import FileResponse

from pensieve.web import auth_routes, insights, items, manage, reader, router, search

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

router.include_router(auth_routes.router)
router.include_router(reader.router)
router.include_router(items.router)
router.include_router(search.router)
router.include_router(manage.router)
router.include_router(insights.router)


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Serve the PWA service worker from the root so it can control the whole origin."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )

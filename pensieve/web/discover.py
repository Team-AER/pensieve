"""Catalog discovery and explicit stream follow/update/unfollow forms. No AI dependency."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from pensieve.models import Feed, StreamFollow
from pensieve.review.evidence import preview_evidence
from pensieve.streams import load_catalog
from pensieve.streams.further import cited_domains, explore_topics, source_suggestions
from pensieve.streams.preview import preview
from pensieve.streams.service import membership, removal_choices, save_choices, unfollow_stream
from pensieve.subscriptions import followed
from pensieve.web.manage import page
from pensieve.web.templating import DB, CsrfUser, CurrentUser

router = APIRouter()


def get_stream(slug):
    catalog = load_catalog()
    stream = next((s for s in catalog.streams if s.slug == slug), None)
    if stream is None:
        raise HTTPException(404, "Unknown stream")
    return catalog, stream


@router.get("/discover")
async def discover_page(
    request: Request,
    user: CurrentUser,
    session: DB,
    q: str = "",
    topic: str = "",
    language: str = "",
    url: str = "",
):
    catalog = load_catalog()
    streams = [
        s
        for s in catalog.streams
        if (not q or q.casefold() in f"{s.title} {s.description} {s.topic}".casefold())
        and (not topic or s.topic == topic)
        and (not language or s.language == language)
    ]
    following = set(await session.scalars(select(Feed.url).where(Feed.user_id == user.id, followed())))
    sample = await preview(url.strip()) if url.strip() else None
    return page(
        request,
        user,
        "discover",
        "discover/index.html",
        {
            "streams": streams,
            "q": q,
            "topic": topic,
            "language": language,
            "url": url,
            "topics": sorted({s.topic for s in catalog.streams}),
            "languages": sorted({s.language for s in catalog.streams}),
            "preview": sample,
            "overlap": await preview_evidence(session, user, sample) if sample else None,
            "cited_domains": await cited_domains(session, user),
            "suggestions": await source_suggestions(
                session, user, {s.topic for s in streams} if (q or topic or language) else None
            ),
            "following": following,
            "memberships": set(
                await session.scalars(select(StreamFollow.slug).where(StreamFollow.user_id == user.id))
            ),
        },
    )


@router.get("/discover/streams/{slug}")
async def stream_page(request: Request, slug: str, user: CurrentUser, session: DB):
    catalog, stream = get_stream(slug)
    current = await membership(session, user, slug)
    following = set(await session.scalars(select(Feed.url).where(Feed.user_id == user.id, followed())))
    sources = []
    for source in stream.sources:
        sample = await preview(source.url)
        sources.append(
            {
                "source": source,
                "preview": sample,
                "url": sample["feed_url"],
                "following": sample["feed_url"] in following or source.url in following,
                "overlap": await preview_evidence(session, user, sample),
                "chosen": current
                and (sample["feed_url"] in current.chosen_urls or source.url in current.chosen_urls),
            }
        )
    urls = {r["url"] for r in sources} | {s.url for s in stream.sources}
    return page(
        request,
        user,
        "discover",
        "discover/stream.html",
        {
            "stream": stream,
            "version": catalog.version,
            "sources": sources,
            "current": current,
            "updated": current and catalog.version > current.catalog_version,
            "retired": sorted(set(current.chosen_urls) - urls) if current else [],
            "removals": await removal_choices(session, user, current) if current else [],
        },
    )


@router.post("/discover/streams/{slug}/follow")
async def follow_stream(request: Request, slug: str, user: CsrfUser, session: DB):
    catalog, stream = get_stream(slug)
    form = await request.form()
    # Values are original catalog URLs; resolve aliases server-side using the preview result.
    selected = set(form.getlist("urls"))
    allowed = {source.url for source in stream.sources}
    if not selected <= allowed:
        raise HTTPException(400, "Choose sources from this stream")
    resolved = {url: (await preview(url))["feed_url"] for url in sorted(allowed)}
    choices = [resolved[url] for url in sorted(selected)]
    await save_choices(
        session, user, stream, catalog.version, choices, form.getlist("retired"), resolved.values()
    )
    await session.commit()
    return RedirectResponse(f"/discover/streams/{slug}", status_code=303)


@router.post("/discover/streams/{slug}/unfollow")
async def remove_stream(request: Request, slug: str, user: CsrfUser, session: DB):
    current = await membership(session, user, slug)
    if current is None:
        return RedirectResponse("/discover", status_code=303)
    form = await request.form()
    await unfollow_stream(session, user, current, form.getlist("urls"))
    await session.commit()
    return RedirectResponse("/discover", status_code=303)


@router.get("/discover/explore")
async def explore_page(
    request: Request,
    user: CurrentUser,
    session: DB,
    item: uuid.UUID | None = None,
    folder: uuid.UUID | None = None,
):
    topics = await explore_topics(session, user, item_id=item, folder_id=folder)
    if topics is None:
        raise HTTPException(404, "Unknown article or folder")
    return page(
        request,
        user,
        "discover",
        "discover/explore.html",
        {
            "suggestions": await source_suggestions(session, user, topics),
            "topics": sorted(topics),
        },
    )


@router.post("/discover/dismiss")
async def dismiss_source(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    url = str(form.get("url") or "")
    if url not in {source.url for stream in load_catalog().streams for source in stream.sources}:
        raise HTTPException(400, "Unknown catalog source")
    dismissed = set((user.settings or {}).get("discovery_dismissed", []))
    dismissed.add(url)
    user.settings = {**(user.settings or {}), "discovery_dismissed": sorted(dismissed)}
    await session.commit()
    return RedirectResponse("/discover", status_code=303)


@router.get("/discover/dismissed")
async def dismissed_page(request: Request, user: CurrentUser):
    return page(
        request,
        user,
        "discover",
        "discover/dismissed.html",
        {
            "dismissed": (user.settings or {}).get("discovery_dismissed", []),
        },
    )


@router.post("/discover/dismissed/reset")
async def reset_dismissals(request: Request, user: CsrfUser, session: DB):
    form = await request.form()
    url = str(form.get("url") or "")
    dismissed = (user.settings or {}).get("discovery_dismissed", [])
    user.settings = {
        **(user.settings or {}),
        "discovery_dismissed": [u for u in dismissed if u != url] if url else [],
    }
    await session.commit()
    return RedirectResponse("/discover/dismissed", status_code=303)

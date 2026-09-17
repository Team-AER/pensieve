"""Feed filing (folder suggestion) and item tagging against the user's controlled vocabulary."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts
from pensieve.ai.client import LLMClient, LLMError, get_client, validate_schema
from pensieve.ai.common import ai_on, utcnow
from pensieve.config import get_settings

log = logging.getLogger(__name__)

AUTO_FILE_CONFIDENCE = 0.9
TAG_BATCH = 5
TAG_TEXT_CHARS = 700  # title + lead paragraph is enough to tag; the fast model is slow on long batches
TAG_TOKENS_PER_ITEM = 160
TAG_TOKENS_HEADROOM = 200
FEW_SHOT_CORRECTIONS = 8

DEFAULT_TAGS: list[tuple[str, str]] = [
    ("ai", "Machine learning, LLMs, model releases, inference"),
    ("programming", "General software craft and languages"),
    ("python", "Python language and ecosystem"),
    ("rust", "Rust language and ecosystem"),
    ("go", "Go language and ecosystem"),
    ("javascript", "JavaScript and TypeScript"),
    ("web", "Browsers, HTML/CSS, web platform"),
    ("databases", "Postgres, SQLite, storage engines, query design"),
    ("devops", "CI/CD, deployment, SRE, observability"),
    ("kubernetes", "Kubernetes and container orchestration"),
    ("containers", "Docker, OCI, container runtimes"),
    ("linux", "Linux kernel, distributions, sysadmin"),
    ("networking", "Protocols, routers, VPNs, DNS"),
    ("security", "Vulnerabilities, exploits, defensive security"),
    ("privacy", "Data protection, surveillance, tracking"),
    ("cloud", "AWS, GCP, Azure, cloud architecture"),
    ("self-hosting", "Homelab and self-hosted services"),
    ("hardware", "CPUs, GPUs, boards, peripherals"),
    ("apple", "Apple platforms and products"),
    ("android", "Android platform and devices"),
    ("mobile", "Mobile development and apps"),
    ("open-source", "OSS projects, licensing, maintainers"),
    ("release", "Version releases and changelogs"),
    ("tooling", "Editors, CLIs, build tools, developer productivity"),
    ("performance", "Benchmarks, optimisation, profiling"),
    ("architecture", "System design and distributed systems"),
    ("data-engineering", "Pipelines, warehouses, streaming"),
    ("science", "Research, physics, biology, mathematics"),
    ("space", "Spaceflight and astronomy"),
    ("business", "Companies, funding, strategy"),
    ("startups", "Founders, launches, early-stage companies"),
    ("policy", "Regulation, government, law affecting tech"),
    ("economics", "Markets, macroeconomics, finance"),
    ("design", "UI/UX, typography, product design"),
    ("gaming", "Video games and game development"),
    ("media", "Journalism, publishing, streaming"),
    ("culture", "Books, essays, society"),
    ("productivity", "Workflows, note-taking, habits"),
    ("career", "Hiring, management, working life"),
    ("photography", "Cameras, imaging, photo tooling"),
]


async def ensure_vocabulary(session: AsyncSession, user: models.User) -> list[models.Tag]:
    """The user's kind='ai' tags, seeding ``DEFAULT_TAGS`` on first use."""
    rows = list(
        (
            await session.scalars(
                select(models.Tag)
                .where(models.Tag.user_id == user.id)
                .order_by(models.Tag.position, models.Tag.name)
            )
        ).all()
    )
    if any(t.kind == "ai" for t in rows):
        return [t for t in rows if t.kind == "ai"]
    existing = {t.name for t in rows}
    values = [
        {
            "id": uuid.uuid4(),
            "user_id": user.id,
            "name": name,
            "kind": "ai",
            "description": desc,
            "position": i,
        }
        for i, (name, desc) in enumerate(DEFAULT_TAGS)
        if name not in existing
    ]
    if values:
        # Concurrent jobs for the same user may seed at once: ON CONFLICT makes this a no-op race.
        await session.execute(
            pg_insert(models.Tag).values(values).on_conflict_do_nothing(constraint="uq_tag_user_name")
        )
    return list(
        (
            await session.scalars(
                select(models.Tag)
                .where(models.Tag.user_id == user.id, models.Tag.kind == "ai")
                .order_by(models.Tag.position, models.Tag.name)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Feed filing
# ---------------------------------------------------------------------------


async def filing_examples(
    session: AsyncSession,
    user: models.User,
    folder_names: dict[uuid.UUID, str],
    limit: int = FEW_SHOT_CORRECTIONS,
) -> list[tuple[str, str | None, str | None]]:
    """Few-shot rows from the user's latest feed_folder corrections: (feed title, AI folder, reader's folder).

    Corrections store folder ids (``folder_id`` field) or free names; ids are resolved through ``folder_names``
    and a folder deleted since is rendered as its id-less "none".
    """
    stmt = (
        select(models.Correction, models.Feed.title)
        .join(models.Feed, models.Feed.id == models.Correction.target_id)
        .where(models.Correction.user_id == user.id, models.Correction.target_type == "feed_folder")
        .order_by(models.Correction.created_at.desc())
        .limit(limit)
    )

    def name(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return folder_names.get(uuid.UUID(value))
        except ValueError:
            return value

    return [(title, name(c.old_value), name(c.new_value)) for c, title in (await session.execute(stmt)).all()]


async def file_feed(
    session: AsyncSession, user: models.User, feed: models.Feed, client: LLMClient | None = None
) -> models.Folder | None:
    """Suggest (and at high confidence apply) a folder for ``feed``. Returns the suggested folder or None.

    A *new* folder is only created when confidence is at least ``AUTO_FILE_CONFIDENCE``; a low-confidence
    proposal for a name that does not exist yet is logged and reported in the job note but not materialised
    (``feeds`` has no free-text column to park the name in; see the package report).
    """
    if not ai_on(user):
        return None
    client = client or get_client()
    settings = get_settings()

    folders = list(
        (
            await session.scalars(
                select(models.Folder).where(models.Folder.user_id == user.id).order_by(models.Folder.position)
            )
        ).all()
    )
    titles = list(
        (
            await session.scalars(
                select(models.Item.title)
                .where(models.Item.feed_id == feed.id)
                .order_by(models.Item.published_at.desc())
                .limit(10)
            )
        ).all()
    )
    by_name = {f.name.lower(): f for f in folders}
    examples = await filing_examples(session, user, {f.id: f.name for f in folders})
    result = await client.chat_json(
        settings.llm_fast_model,
        prompts.FEED_FILING_SYSTEM,
        prompts.feed_filing_user(feed.title, feed.description, titles, [f.name for f in folders], examples),
        prompts.FEED_FILING_SCHEMA,
        max_tokens=200,
        workflow="file_feed",
        name="feed_filing",
    )
    confidence = float(result.get("confidence") or 0.0)
    target: models.Folder | None = None
    folder_name = (result.get("folder") or "").strip()
    new_name = (result.get("new_folder") or "").strip()
    if folder_name and folder_name.lower() in by_name:
        target = by_name[folder_name.lower()]
    elif new_name or folder_name:
        name = (new_name or folder_name)[:120]
        if name.lower() in by_name:
            target = by_name[name.lower()]
        elif confidence >= AUTO_FILE_CONFIDENCE:
            target = models.Folder(user_id=user.id, name=name, position=len(folders), ai_suggested=True)
            session.add(target)
            await session.flush()
        else:
            log.info("file_feed: proposed new folder %r for feed %s at %.2f; not created", name, feed.id, confidence)
    if target is None:
        feed.suggested_folder_id = None
        feed.suggested_folder_confidence = confidence
        await session.flush()
        return None

    feed.suggested_folder_id = target.id
    feed.suggested_folder_confidence = confidence
    if confidence >= AUTO_FILE_CONFIDENCE and ai_on(user, "auto_file") and feed.folder_id is None:
        feed.folder_id = target.id
    await session.flush()
    return target


async def dismiss_folder_suggestion(session: AsyncSession, user: models.User, feed: models.Feed) -> None:
    """Clear the feed's folder suggestion and record it as a feed_folder correction (old=folder id, new=None)."""
    if feed.user_id != user.id:
        return
    if feed.suggested_folder_id is not None:
        session.add(
            models.Correction(
                user_id=user.id,
                target_type="feed_folder",
                target_id=feed.id,
                field="folder_id",
                old_value=str(feed.suggested_folder_id),
                new_value=None,
            )
        )
    feed.suggested_folder_id = None
    feed.suggested_folder_confidence = None
    await session.flush()


# ---------------------------------------------------------------------------
# Item tagging
# ---------------------------------------------------------------------------


async def tagging_examples(
    session: AsyncSession, user: models.User, limit: int = FEW_SHOT_CORRECTIONS
) -> list[tuple[str, list[str], list[str]]]:
    """Few-shot rows from the user's latest item_tag corrections: (title, ai tags, chosen tags)."""
    stmt = (
        select(models.Correction, models.Item.title)
        .join(models.Item, models.Item.id == models.Correction.target_id)
        .where(models.Correction.user_id == user.id, models.Correction.target_type == "item_tag")
        .order_by(models.Correction.created_at.desc())
        .limit(limit)
    )
    out = []
    for corr, title in (await session.execute(stmt)).all():
        out.append((title, _split_tags(corr.old_value), _split_tags(corr.new_value)))
    return out


def _split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [t.strip() for t in value.replace(";", ",").split(",") if t.strip()]


def is_tagged(row: models.ItemAI | None) -> bool:
    """True when ``row`` carries tagging output from the current prompt version (a summary-only row does not)."""
    return (
        row is not None
        and row.prompt_version == prompts.PROMPT_VERSION
        and (bool(row.tags) or row.content_type is not None)
    )


def _entry_ok(entry: object) -> bool:
    try:
        validate_schema(entry, prompts.ITEM_TAGGING_ENTRY_SCHEMA)
    except LLMError as exc:
        log.warning("tag_items: skipping malformed entry: %s", exc)
        return False
    return True


async def tag_items(
    session: AsyncSession,
    user: models.User,
    items: list[models.Item],
    client: LLMClient | None = None,
    *,
    force: bool = False,
) -> list[models.ItemAI]:
    """Tag ``items`` in batches of ``TAG_BATCH``; writes/updates ``item_ai`` rows. Respects ``tag_items`` toggle.

    Items already tagged at the current ``PROMPT_VERSION`` are skipped unless ``force`` (a retried job must not
    re-pay for batches that succeeded). A malformed entry in a batch is skipped, not the batch.
    """
    if not items or not ai_on(user, "tag_items"):
        return []
    client = client or get_client()
    settings = get_settings()
    existing: dict[uuid.UUID, models.ItemAI] = {
        row.item_id: row
        for row in (
            await session.scalars(
                select(models.ItemAI).where(
                    models.ItemAI.user_id == user.id, models.ItemAI.item_id.in_([i.id for i in items])
                )
            )
        ).all()
    }
    todo = items if force else [i for i in items if not is_tagged(existing.get(i.id))]
    if not todo:
        return []
    vocab = await ensure_vocabulary(session, user)
    vocab_names = [t.name for t in vocab]
    vocab_lookup = {n.lower(): n for n in vocab_names}
    examples = await tagging_examples(session, user)

    written: list[models.ItemAI] = []
    for start in range(0, len(todo), TAG_BATCH):
        batch = todo[start : start + TAG_BATCH]
        payload = [(i, it.title, (it.content_text or "")[:TAG_TEXT_CHARS]) for i, it in enumerate(batch)]
        try:
            result = await client.chat_json(
                settings.llm_fast_model,
                prompts.ITEM_TAGGING_SYSTEM,
                prompts.item_tagging_user(vocab_names, payload, examples),
                prompts.ITEM_TAGGING_SCHEMA,
                max_tokens=TAG_TOKENS_PER_ITEM * len(batch) + TAG_TOKENS_HEADROOM,
                workflow="tag_items",
                name="item_tagging",
                validate_with=prompts.ITEM_TAGGING_BATCH_SCHEMA,
            )
        except LLMError as exc:
            log.warning("tag_items batch failed for user %s: %s", user.id, exc)
            raise
        by_index = {int(entry["index"]): entry for entry in result["items"] if _entry_ok(entry)}
        values: list[dict] = []
        for i, item in enumerate(batch):
            entry = by_index.get(i)
            if entry is None:
                continue
            tags: list[str] = []
            confidences: dict[str, float] = {}
            for t in sorted(entry["tags"], key=lambda t: -float(t["confidence"])):
                name = vocab_lookup.get(str(t["name"]).strip().lower())
                conf = float(t["confidence"])
                if not name or conf < prompts.MIN_TAG_CONFIDENCE or name in tags:
                    continue
                tags.append(name)
                confidences[name] = round(conf, 3)
                if len(tags) >= prompts.MAX_TAGS_PER_ITEM:
                    break
            content_type = entry["content_type"] if entry["content_type"] in prompts.CONTENT_TYPES else None
            values.append(
                {
                    "user_id": user.id,
                    "item_id": item.id,
                    "tags": tags,
                    "confidences": confidences,
                    "content_type": content_type,
                    "model": settings.llm_fast_model,
                    "prompt_version": prompts.PROMPT_VERSION,
                    "generated_at": utcnow(),
                }
            )
        if not values:
            continue
        # Upsert rather than add(): two jobs can tag the same item at once (a backfill chunk and the
        # fetch-time job, or a retry racing its predecessor), and the pkey (user_id, item_id) would raise.
        stmt = pg_insert(models.ItemAI).values(values)
        updatable = ("tags", "confidences", "content_type", "model", "prompt_version", "generated_at")
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "item_id"], set_={k: getattr(stmt.excluded, k) for k in updatable}
        )
        await session.execute(stmt)
        # each batch is durable on its own: a later batch failing must not lose this one on retry
        rows = (
            await session.scalars(
                select(models.ItemAI)
                .where(models.ItemAI.user_id == user.id, models.ItemAI.item_id.in_([v["item_id"] for v in values]))
                .execution_options(populate_existing=True)
            )
        ).all()
        for row in rows:
            existing[row.item_id] = row
        written.extend(rows)
    return written

"""Feed filing (folder suggestion) and item tagging against the user's controlled vocabulary."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pensieve import models
from pensieve.ai import prompts
from pensieve.ai.client import LLMClient, LLMError, get_client
from pensieve.ai.common import ai_on, utcnow
from pensieve.config import get_settings

log = logging.getLogger(__name__)

AUTO_FILE_CONFIDENCE = 0.9
TAG_BATCH = 10
TAG_TEXT_CHARS = 1500
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
    seeded = [
        models.Tag(user_id=user.id, name=name, kind="ai", description=desc, position=i)
        for i, (name, desc) in enumerate(DEFAULT_TAGS)
        if name not in existing
    ]
    session.add_all(seeded)
    await session.flush()
    return seeded


# ---------------------------------------------------------------------------
# Feed filing
# ---------------------------------------------------------------------------


async def file_feed(
    session: AsyncSession, user: models.User, feed: models.Feed, client: LLMClient | None = None
) -> models.Folder | None:
    """Suggest (and at high confidence apply) a folder for ``feed``. Returns the suggested folder or None."""
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
    result = await client.chat_json(
        settings.llm_fast_model,
        prompts.FEED_FILING_SYSTEM,
        prompts.feed_filing_user(feed.title, feed.description, titles, [f.name for f in folders]),
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
        else:
            target = models.Folder(user_id=user.id, name=name, position=len(folders), ai_suggested=True)
            session.add(target)
            await session.flush()
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


async def tag_items(
    session: AsyncSession, user: models.User, items: list[models.Item], client: LLMClient | None = None
) -> list[models.ItemAI]:
    """Tag ``items`` in batches of 10; writes/updates ``item_ai`` rows. Respects ``tag_items`` toggle."""
    if not items or not ai_on(user, "tag_items"):
        return []
    client = client or get_client()
    settings = get_settings()
    vocab = await ensure_vocabulary(session, user)
    vocab_names = [t.name for t in vocab]
    vocab_lookup = {n.lower(): n for n in vocab_names}
    examples = await tagging_examples(session, user)

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
    written: list[models.ItemAI] = []
    for start in range(0, len(items), TAG_BATCH):
        batch = items[start : start + TAG_BATCH]
        payload = [(i, it.title, (it.content_text or "")[:TAG_TEXT_CHARS]) for i, it in enumerate(batch)]
        try:
            result = await client.chat_json(
                settings.llm_fast_model,
                prompts.ITEM_TAGGING_SYSTEM,
                prompts.item_tagging_user(vocab_names, payload, examples),
                prompts.ITEM_TAGGING_SCHEMA,
                max_tokens=120 * len(batch) + 100,
                workflow="tag_items",
                name="item_tagging",
            )
        except LLMError as exc:
            log.warning("tag_items batch failed for user %s: %s", user.id, exc)
            raise
        by_index = {int(entry["index"]): entry for entry in result["items"]}
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
            row = existing.get(item.id)
            if row is None:
                row = models.ItemAI(user_id=user.id, item_id=item.id)
                session.add(row)
                existing[item.id] = row
            row.tags = tags
            row.confidences = confidences
            row.content_type = content_type
            row.model = settings.llm_fast_model
            row.prompt_version = prompts.PROMPT_VERSION
            row.generated_at = utcnow()
            written.append(row)
    await session.flush()
    return written

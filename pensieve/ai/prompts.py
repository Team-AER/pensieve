"""Versioned prompts and JSON schemas for every AI workflow.

Bump ``PROMPT_VERSION`` whenever wording or a schema changes; ``item_ai.prompt_version`` records which one
produced a row so stale output can be regenerated selectively.
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "2026-09-18.1"

CONTENT_TYPES = [
    "article",
    "release_note",
    "tutorial",
    "opinion",
    "announcement",
    "listicle",
    "podcast",
    "video",
]

MAX_TAGS_PER_ITEM = 3
MIN_TAG_CONFIDENCE = 0.5

# ---------------------------------------------------------------------------
# Schemas (strict: additionalProperties false, every property required; nullable via anyOf)
# ---------------------------------------------------------------------------


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


FEED_FILING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "folder": _nullable({"type": "string"}),
        "new_folder": _nullable({"type": "string"}),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["folder", "new_folder", "confidence"],
    "additionalProperties": False,
}

ITEM_TAGGING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["name", "confidence"],
                            "additionalProperties": False,
                        },
                    },
                    "content_type": {"type": "string", "enum": CONTENT_TYPES},
                },
                "required": ["index", "tags", "content_type"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}
"""Batch form: one entry per input item. A single item's shape is {tags:[{name, confidence}], content_type}."""

CLUSTER_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"same_story": {"type": "boolean"}, "headline": {"type": "string"}},
    "required": ["same_story", "headline"],
    "additionalProperties": False,
}

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"profile_text": {"type": "string"}},
    "required": ["profile_text"],
    "additionalProperties": False,
}

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "top_stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"cluster_ref": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["cluster_ref", "why"],
                "additionalProperties": False,
            },
        },
        "safe_to_skip_reason": {"type": "string"},
    },
    "required": ["summary", "top_stories", "safe_to_skip_reason"],
    "additionalProperties": False,
}

WEEKLY_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "highlights", "suggestions"],
    "additionalProperties": False,
}

ITEM_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bullets": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3},
        "why_it_matters": {"type": "string"},
    },
    "required": ["bullets", "why_it_matters"],
    "additionalProperties": False,
}

ASK_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_JSON_ONLY = "Respond with a single JSON object matching the schema and nothing else."

FEED_FILING_SYSTEM = (
    "You file RSS feeds into a reader's folders. Given a feed and the reader's existing folder names, pick the "
    "single best existing folder, or propose one short new folder name (2-3 words, Title Case) when nothing "
    "fits. Prefer existing folders. Set 'folder' to an existing name or null; set 'new_folder' only when 'folder' "
    "is null. Confidence is 0-1: 0.9+ means you would be surprised to be wrong. " + _JSON_ONLY
)

ITEM_TAGGING_SYSTEM = (
    "You tag articles for a technical reader using ONLY the provided tag vocabulary (exact names). Give each item "
    f"at most {MAX_TAGS_PER_ITEM} tags with a 0-1 confidence, and classify its content_type as one of: "
    + ", ".join(CONTENT_TYPES)
    + ". Omit tags you are unsure about rather than guessing. Return one entry per item, keyed by index. "
    + _JSON_ONLY
)

CLUSTER_CONFIRM_SYSTEM = (
    "You decide whether two feed items report the same underlying story (same event, release, or announcement "
    "from different sources), not merely the same topic. If they do, write one neutral headline (under 12 words) "
    "that covers both. If not, headline may repeat item A's title. " + _JSON_ONLY
)

PROFILE_SYSTEM = (
    "You maintain a plain-text reader profile for a self-hosted RSS reader. From the reading signals, write at "
    "most 40 short lines describing what this reader follows closely, what they skim or skip, tools and topics "
    "they care about, recurring sources they open, and stated corrections to the AI. Write in second person "
    "('You follow ...'), concrete, no fluff, no markdown headings. Keep lines that the previous profile got right. "
    + _JSON_ONLY
)

DIGEST_SYSTEM = (
    "You write a morning digest for one reader. Given ranked stories (each with a ref number, sources, tags and "
    "an affinity score) and the reader profile, write a 2-4 sentence summary of the day, then for the top "
    "stories a one-line 'why' tailored to the reader. Return cluster_ref numbers exactly as given. Also give "
    "one sentence explaining why the low-affinity remainder is safe to skip. " + _JSON_ONLY
)

WEEKLY_REVIEW_SYSTEM = (
    "You write a short weekly review of a reader's RSS consumption from the statistics provided: what they read "
    "most, which sources earned their attention, rising and fading topics, and 2-4 concrete suggestions "
    "(e.g. sources to mute or folders to split). Plain sentences, no markdown. " + _JSON_ONLY
)

ITEM_SUMMARY_SYSTEM = (
    "Summarise the article in exactly three crisp bullets (facts, not opinions), then one or two sentences on why "
    "it matters to this specific reader given their profile. If the profile is empty, explain why it matters to "
    "a technical reader in general. " + _JSON_ONLY
)

ASK_SYSTEM = (
    "Answer the reader's question using ONLY the numbered excerpts from things they have read. Cite sources "
    "inline as [n] using the excerpt numbers, and list every cited number in 'citations'. If the excerpts do not "
    "contain the answer, say so plainly. " + _JSON_ONLY
)

# ---------------------------------------------------------------------------
# User-message builders
# ---------------------------------------------------------------------------


def feed_filing_user(title: str, description: str, item_titles: list[str], folder_names: list[str]) -> str:
    lines = [f"Feed title: {title or '(untitled)'}", f"Feed description: {description or '(none)'}", ""]
    lines.append("Recent item titles:")
    lines.extend(f"- {t}" for t in item_titles[:10])
    lines.append("")
    lines.append("Existing folders: " + (", ".join(folder_names) if folder_names else "(none yet)"))
    return "\n".join(lines)


def format_tag_examples(examples: list[tuple[str, list[str], list[str]]]) -> str:
    """Few-shot block from corrections: (item title, tags the AI gave, tags the reader chose)."""
    if not examples:
        return ""
    lines = ["The reader corrected earlier tagging. Learn from these:"]
    for title, old, new in examples:
        lines.append(
            f'- "{title}": AI said [{", ".join(old) or "none"}] -> reader chose [{", ".join(new) or "none"}]'
        )
    return "\n".join(lines) + "\n\n"


def item_tagging_user(
    vocabulary: list[str],
    items: list[tuple[int, str, str]],
    examples: list[tuple[str, list[str], list[str]]] | None = None,
) -> str:
    """``items`` are (index, title, text)."""
    parts = ["Tag vocabulary: " + ", ".join(vocabulary), "", format_tag_examples(examples or []).rstrip(), ""]
    for index, title, text in items:
        parts.append(f"### Item {index}")
        parts.append(f"Title: {title}")
        parts.append(f"Text: {text}")
        parts.append("")
    return "\n".join(p for p in parts if p is not None).strip()


def cluster_confirm_user(
    title_a: str, text_a: str, feed_a: str, title_b: str, text_b: str, feed_b: str
) -> str:
    return (
        f"Item A ({feed_a}): {title_a}\n{text_a}\n\n"
        f"Item B ({feed_b}): {title_b}\n{text_b}\n\n"
        "Are A and B the same story?"
    )


def profile_user(previous: str, signals: dict[str, Any]) -> str:
    return (
        "Previous profile:\n" + (previous or "(none)") + "\n\n"
        "Reading signals from the last 90 days (JSON):\n" + json.dumps(signals, indent=1, default=str)
    )


def digest_user(profile: str, stories: list[dict[str, Any]], skip_candidates: list[str]) -> str:
    return (
        "Reader profile:\n" + (profile or "(none yet)") + "\n\n"
        "Ranked stories (JSON):\n" + json.dumps(stories, indent=1, default=str) + "\n\n"
        "Low-affinity remainder (titles):\n" + "\n".join(f"- {t}" for t in skip_candidates[:20])
    )


def weekly_review_user(profile: str, stats: dict[str, Any]) -> str:
    return (
        "Reader profile:\n" + (profile or "(none yet)") + "\n\n"
        "Week statistics (JSON):\n" + json.dumps(stats, indent=1, default=str)
    )


def item_summary_user(profile: str, title: str, text: str) -> str:
    return f"Reader profile:\n{profile or '(none)'}\n\nArticle title: {title}\n\nArticle text:\n{text}"


def ask_user(question: str, excerpts: list[tuple[int, str, str, str]]) -> str:
    """``excerpts`` are (n, title, source, text)."""
    block = "\n\n".join(f"[{n}] {title} ({source})\n{text}" for n, title, source, text in excerpts)
    return f"Question: {question}\n\nExcerpts:\n{block}"


__all__ = [
    "ASK_ANSWER_SCHEMA",
    "ASK_SYSTEM",
    "CLUSTER_CONFIRM_SCHEMA",
    "CLUSTER_CONFIRM_SYSTEM",
    "CONTENT_TYPES",
    "DIGEST_SCHEMA",
    "DIGEST_SYSTEM",
    "FEED_FILING_SCHEMA",
    "FEED_FILING_SYSTEM",
    "ITEM_SUMMARY_SCHEMA",
    "ITEM_SUMMARY_SYSTEM",
    "ITEM_TAGGING_SCHEMA",
    "ITEM_TAGGING_SYSTEM",
    "MAX_TAGS_PER_ITEM",
    "MIN_TAG_CONFIDENCE",
    "PROFILE_SCHEMA",
    "PROFILE_SYSTEM",
    "PROMPT_VERSION",
    "WEEKLY_REVIEW_SCHEMA",
    "WEEKLY_REVIEW_SYSTEM",
]

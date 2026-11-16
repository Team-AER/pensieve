"""Versioned, in-memory editorial catalog. No catalog tables or background subscriptions."""

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from pensieve.config import get_settings


class Source(BaseModel):
    url: str
    rationale: str = Field(min_length=1)
    membership: Literal["light", "comprehensive"]

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("Catalog sources must be HTTP(S) URLs without credentials")
        if len(value) > 2048:
            raise ValueError("URL exceeds feed limit")
        return value


class Stream(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=120)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    language: str = Field(min_length=2)
    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({s.url for s in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source in a stream")
        return self


class Catalog(BaseModel):
    version: int = Field(ge=1)
    streams: list[Stream] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_streams(self):
        if len({s.slug for s in self.streams}) != len(self.streams):
            raise ValueError("Duplicate stream slug")
        return self


def catalog_path():
    return (
        Path(get_settings().stream_catalog_path)
        if get_settings().stream_catalog_path
        else Path(__file__).with_name("catalog.toml")
    )


@lru_cache(maxsize=4)
def _load(path, mtime):
    with Path(path).open("rb") as file:
        return Catalog.model_validate(tomllib.load(file))


def load_catalog():
    path = catalog_path()
    return _load(str(path), path.stat().st_mtime_ns)

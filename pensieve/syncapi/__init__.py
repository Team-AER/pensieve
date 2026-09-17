"""Sync protocols for native clients: Google Reader API and Fever API. Exposes `router`."""

from fastapi import APIRouter

router = APIRouter()

from pensieve.syncapi import fever, greader  # noqa: F401  (register sub-routers on `router`)

"""Opt-in GlitchTip error reporting using the Sentry-compatible SDK."""

from __future__ import annotations

import os


def scrub_event(event, hint):
    """Retain diagnostic stacks without application content or request credentials."""
    for key in ("request", "user", "breadcrumbs", "extra"):
        event.pop(key, None)
    if "logentry" in event:
        event["logentry"] = {"formatted": "Application error (content redacted)"}
    event["server_name"] = "pensieve"
    for item in event.get("exception", {}).get("values", []):
        item["value"] = item.get("type", "Error")
        for frame in item.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def configure_monitoring():
    """An empty DSN disables collection and imports no SDK."""
    dsn = os.getenv("PENSIEVE_GLITCHTIP_DSN", os.getenv("SENTRY_DSN", "")).strip()
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.arq import ArqIntegration
    from sentry_sdk.integrations.atexit import AtexitIntegration
    from sentry_sdk.integrations.excepthook import ExcepthookIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    from sentry_sdk.integrations.threading import ThreadingIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE") or "pensieve@0.1.0",
        default_integrations=False,
        integrations=[
            AtexitIntegration(),
            ExcepthookIntegration(),
            ThreadingIntegration(),
            LoggingIntegration(level=None, event_level=40),
            FastApiIntegration(),
            StarletteIntegration(),
            ArqIntegration(),
        ],
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        auto_session_tracking=False,
        traces_sample_rate=0.0,
        before_send=scrub_event,
    )
    return True

"""Monitoring must never attach application content to error events."""

import importlib.util
from pathlib import Path


def test_monitoring_removes_private_content():
    path = Path(__file__).resolve().parents[1] / "pensieve/monitoring.py"
    spec = importlib.util.spec_from_file_location("monitoring_under_test", path)
    monitoring = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitoring)
    event = {
        "request": {"data": "PRIVATE_CONTENT"},
        "user": {"email": "PRIVATE_CONTENT"},
        "extra": {"token": "PRIVATE_CONTENT"},
        "breadcrumbs": [{"message": "PRIVATE_CONTENT"}],
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "PRIVATE_CONTENT",
                    "stacktrace": {"frames": [{"filename": "app.py", "vars": {"secret": "PRIVATE_CONTENT"}}]},
                }
            ]
        },
    }
    result = monitoring.scrub_event(event, {})
    assert "PRIVATE_CONTENT" not in repr(result)
    assert result["exception"]["values"][0]["stacktrace"]["frames"][0]["filename"] == "app.py"


def test_empty_dsn_disables_monitoring(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "pensieve/monitoring.py"
    spec = importlib.util.spec_from_file_location("monitoring_under_test", path)
    monitoring = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitoring)
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("PENSIEVE_GLITCHTIP_DSN", "")
    assert monitoring.configure_monitoring() is False

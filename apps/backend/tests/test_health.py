"""Health-check and scaffold sanity tests for task 1.1."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_health_endpoint_returns_ok() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_caldera_defaults_and_env_override(monkeypatch) -> None:
    """Caldera URL/key default to the lab values and come from PIPELINE_UI_* env."""
    from app.core.config import Settings

    monkeypatch.delenv("PIPELINE_UI_CALDERA_URL", raising=False)
    monkeypatch.delenv("PIPELINE_UI_CALDERA_KEY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.caldera_url == "http://localhost:8888"
    assert settings.caldera_key == "ADMIN123"
    assert settings.caldera_headers == {"KEY": "ADMIN123"}

    monkeypatch.setenv("PIPELINE_UI_CALDERA_URL", "http://caldera:8888")
    monkeypatch.setenv("PIPELINE_UI_CALDERA_KEY", "OTHER")
    overridden = Settings(_env_file=None)
    assert overridden.caldera_url == "http://caldera:8888"
    assert overridden.caldera_headers == {"KEY": "OTHER"}


def test_database_url_defaults_to_postgres_in_dev() -> None:
    settings = get_settings()
    assert settings.database_url.startswith("postgresql")

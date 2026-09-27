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


def test_settings_reuse_sticks_caldera_values() -> None:
    """Config must read Caldera values from the existing sticks config,
    not redefine them."""
    settings = get_settings()

    # Reused from sticks/config/config.py (CALDERA_URL + CALDERA_API_KEY_RED).
    assert settings.caldera_url == "http://localhost:8888"
    assert settings.caldera_key == "ADMIN123"
    assert settings.caldera_headers == {"KEY": "ADMIN123"}


def test_database_url_defaults_to_sqlite_in_dev() -> None:
    settings = get_settings()
    assert settings.database_url.startswith("sqlite")

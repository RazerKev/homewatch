"""Tests for app/config.py, the settings the API and watchdog read when they start."""

import pytest
from pydantic import ValidationError

from app.config import Settings

DB = "postgresql+psycopg://user:pass@localhost:5432/test"  # any value: nothing connects in these tests

def test_rejects_ntfy_url_without_https():
    # The pilot bug: NTFY_URL wasn't a full web address, so every alert failed to send.
    with pytest.raises(ValidationError):
        Settings(database_url=DB, ntfy_url="ntfy.sh/homewatch-test")

def test_error_does_not_show_the_topic():
    # The topic works like a password, so it must never end up in a log.
    with pytest.raises(ValidationError) as error:
        Settings(database_url=DB, ntfy_url="ntfy,sh/secret-topic-123")
    assert "secret-topic-123" not in str(error.value)

def test_accepts_full_ntfy_url():
    # The pilot bug: NTFY_URL wasn't a full web address, so every alert failed to send.
    settings = Settings(database_url=DB, ntfy_url="https://ntfy.sh/homewatch-test")
    assert settings.ntfy_url.get_secret_value() == "https://ntfy.sh/homewatch-test"  # round-trip through SecretStr

def test_blank_ntfy_url_means_no_phone_alerts():
    # The pilot bug: Docker Compose passes unset variables as empty strings, so the validator must convert "" to None.
    settings = Settings(database_url=DB, ntfy_url="")
    assert settings.ntfy_url is None
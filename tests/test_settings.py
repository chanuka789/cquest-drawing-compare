"""Settings loading and defaults."""

from __future__ import annotations

import pytest

from engine.settings import Settings, get_settings


def test_defaults_are_safe(monkeypatch):
    # The session fixture sets these; clear them to see the real defaults.
    for name in ("CQDC_DEV", "CQDC_LOG_LEVEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.app_name == "C-Quest Drawing Compare"
    assert settings.dev_mode is False  # production unless told otherwise
    assert settings.log_level == "INFO"
    assert settings.anthropic_api_key is None
    assert settings.ai_available is False  # offline is the default


@pytest.mark.parametrize("raw", ["1", "true", "True", "yes", "on"])
def test_cqdc_dev_turns_on_dev_mode(monkeypatch, raw):
    monkeypatch.setenv("CQDC_DEV", raw)
    assert Settings(_env_file=None).dev_mode is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_cqdc_dev_off_means_production(monkeypatch, raw):
    monkeypatch.setenv("CQDC_DEV", raw)
    assert Settings(_env_file=None).dev_mode is False


def test_log_level_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("CQDC_LOG_LEVEL", "DEBUG")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_an_invalid_log_level_is_rejected(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setenv("CQDC_LOG_LEVEL", "CHATTY")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_a_blank_api_key_means_offline(monkeypatch):
    """An empty key in .env must not read as a broken key."""
    for blank in ("", "   "):
        monkeypatch.setenv("ANTHROPIC_API_KEY", blank)
        settings = Settings(_env_file=None)
        assert settings.anthropic_api_key is None
        assert settings.ai_available is False


def test_a_real_key_makes_cloud_ai_possible(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    assert Settings(_env_file=None).ai_available is True


def test_get_settings_is_cached():
    assert get_settings() is get_settings()

import pytest
from pydantic import ValidationError

from app.config import Config, load_config
from tests.conftest import CONFIG


def test_example_config_loads_with_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DEZHURKA_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("DEZHURKA_ADMIN_PASSWORD", "secret1")
    monkeypatch.setenv("DEZHURKA_KEY_PAYMENTS", "k" * 20)
    monkeypatch.delenv("DEZHURKA_TELEGRAM_TOKEN", raising=False)
    cfg = load_config("config.example.yaml")
    assert cfg.telegram.bot_token == "" and cfg.smtp.host == ""
    assert cfg.service("payments-api").integration_key == "k" * 20


def test_missing_secret_is_rejected(monkeypatch):
    monkeypatch.delenv("DEZHURKA_SECRET_KEY", raising=False)
    monkeypatch.setenv("DEZHURKA_ADMIN_PASSWORD", "secret1")
    monkeypatch.setenv("DEZHURKA_KEY_PAYMENTS", "k" * 20)
    with pytest.raises(ValidationError):
        load_config("config.example.yaml")


def test_broken_references_are_reported():
    bad = {**CONFIG, "escalation_policies": [{"id": "default", "steps": [{"targets": [{"user": "ghost"}]}]}]}
    with pytest.raises(ValidationError, match="ghost"):
        Config.model_validate(bad)

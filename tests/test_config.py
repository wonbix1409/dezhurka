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


def test_telegram_proxy_validation():
    import pytest
    from pydantic import ValidationError

    from app.config import Telegram

    assert Telegram(proxy="socks5h://host.docker.internal:10808").proxy == "socks5h://host.docker.internal:10808"
    assert Telegram(proxy=" http://user:pw@10.0.0.5:3128 ").proxy == "http://user:pw@10.0.0.5:3128"
    assert Telegram().proxy == ""
    for bad in ("10.0.0.5:1080", "ftp://x:1", "socks5://host:port/../x y"):
        with pytest.raises(ValidationError):
            Telegram(proxy=bad)


def test_telegram_api_uses_proxy_and_hides_credentials():
    import asyncio

    from app.notify.telegram import TelegramAPI, proxy_for_log

    # без пакета socksio httpx падает уже здесь — проверяем, что зависимость на месте
    api = TelegramAPI("123:abc", proxy="socks5://u:secret@127.0.0.1:1080")
    try:
        assert api.proxy == "socks5://u:secret@127.0.0.1:1080"
        assert any(t is not None for t in api.client._mounts.values()), "прокси не подключён к клиенту"
    finally:
        asyncio.run(api.close())
    assert proxy_for_log("socks5://u:secret@127.0.0.1:1080") == "socks5://***@127.0.0.1:1080"
    assert proxy_for_log("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"

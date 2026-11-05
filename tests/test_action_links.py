"""Ссылки «Беру» / «Решено» из писем."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dezhurka.action_links import LinkError, make_token, read_token
from dezhurka.config import Config
from dezhurka.main import create_app
from dezhurka.models import Incident, Status, utcnow
from dezhurka.notify.base import Contact
from dezhurka.notify.dispatcher import MultiDispatcher
from tests.conftest import CONFIG

SECRET = CONFIG["settings"]["secret_key"]
NOW = datetime(2026, 11, 5, 10, 0)


def test_token_roundtrip():
    t = make_token(SECRET, 7, "ack", "ivan", NOW)
    c = read_token(SECRET, t, NOW + timedelta(hours=23))
    assert (c.incident_id, c.action, c.user_id) == (7, "ack", "ivan")
    assert c.expires == NOW + timedelta(hours=24)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda t: t[:-2] + ("AA" if t[-2:] != "AA" else "BB"), "подделана"),  # чужая подпись
        (lambda t: "x" + t, "подделана"),  # изменён payload
        (lambda t: t.replace(".", ""), "повреждена"),
        (lambda t: t + ".extra", "повреждена"),
    ],
)
def test_token_tampering_rejected(mutate, message):
    t = make_token(SECRET, 7, "ack", "ivan", NOW)
    with pytest.raises(LinkError, match=message):
        read_token(SECRET, mutate(t), NOW)


def test_token_expired_and_wrong_secret():
    t = make_token(SECRET, 7, "resolve", "ivan", NOW)
    with pytest.raises(LinkError, match="истёк"):
        read_token(SECRET, t, NOW + timedelta(hours=24))
    with pytest.raises(LinkError):
        read_token("другой-секрет-1234567", t, NOW)
    with pytest.raises(LinkError):
        make_token(SECRET, 7, "delete", "ivan", NOW)


@pytest.fixture
def client(tmp_path):
    cfg = dict(CONFIG)
    cfg["settings"] = {**CONFIG["settings"], "database_url": f"sqlite+aiosqlite:///{tmp_path}/links.db"}
    app = create_app(Config.model_validate(cfg), start_background=False)
    with TestClient(app) as c:
        yield c


def _incident(client) -> int:
    r = client.post("/api/v1/ingest/api-key-0123456789/generic", json={"title": "Диск /var заполнен"})
    return r.json()["results"][0]["incident_id"]


def _status(client, iid):
    async def get():
        async with client.app.state.engine.sessions() as s:
            inc = await s.get(Incident, iid)
            return inc.status, inc.acknowledged_by, inc.resolved_by

    return client.portal.call(get)  # в цикле событий приложения


def test_link_flow_get_is_safe_post_acts(client):
    iid = _incident(client)
    token = make_token(SECRET, iid, "ack", "petr", utcnow())
    page = client.get(f"/a/{token}")
    assert page.status_code == 200
    assert "Беру" in page.text and "Пётр" in page.text and "Диск /var заполнен" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "Графики" not in page.text  # без меню панели
    assert _status(client, iid)[0] == Status.TRIGGERED  # открытие ссылки ничего не меняет

    done = client.post(f"/a/{token}")
    assert done.status_code == 200 and "Взято в работу" in done.text
    assert _status(client, iid)[:2] == (Status.ACKNOWLEDGED, "Пётр")

    resolve = make_token(SECRET, iid, "resolve", "petr", utcnow())
    assert client.post(f"/a/{resolve}").status_code == 200
    assert _status(client, iid) == (Status.RESOLVED, "Пётр", "Пётр")
    # повторное действие по уже закрытому инциденту — понятная ошибка, а не 500
    again = client.post(f"/a/{resolve}")
    assert again.status_code == 409 and "уже решён" in again.text


def test_bad_links(client):
    iid = _incident(client)
    expired = make_token(SECRET, iid, "ack", "petr", utcnow() - timedelta(days=2))
    r = client.get(f"/a/{expired}")
    assert r.status_code == 400 and "истёк" in r.text
    assert client.post(f"/a/{expired}").status_code == 400
    assert client.get("/a/garbage").status_code == 400
    ghost = make_token(SECRET, iid, "ack", "nobody", utcnow())
    assert "больше нет" in client.get(f"/a/{ghost}").text
    missing = make_token(SECRET, 99999, "ack", "petr", utcnow())
    assert "не найден" in client.get(f"/a/{missing}").text
    assert _status(client, iid)[0] == Status.TRIGGERED


class FakeEmail:
    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, body, html=None):
        self.sent.append((to, subject, body, html))
        return None


def _inc(status: str) -> Incident:
    return Incident(id=5, service_id="api", dedup_key="k", title="T", details="", severity="critical",
                    source="generic", status=status)


def test_email_contains_action_links():
    email = FakeEmail()
    d = MultiDispatcher("https://dz.example/", None, email, link_secret=SECRET)
    contact = Contact(user_id="anna", name="Анна", email="anna@example.com", telegram_chat_id=None)
    asyncio.run(d.page(contact, _inc(Status.TRIGGERED), "API", "Вы дежурите"))
    body = email.sent[0][2]
    lines = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in body.splitlines() if ": https://" in line}
    assert set(lines) >= {"Беру", "Решено", "Открыть в панели"}
    claim = read_token(SECRET, lines["Беру"].rsplit("/a/", 1)[1], utcnow())
    assert (claim.incident_id, claim.action, claim.user_id) == (5, "ack", "anna")
    assert lines["Беру"].startswith("https://dz.example/a/")  # без двойного слэша

    assert [a for a, _ in d.email_actions(_inc(Status.ACKNOWLEDGED), "anna")] == ["Решено"]
    assert d.email_actions(_inc(Status.RESOLVED), "anna") == []
    assert MultiDispatcher("https://x", None, email).email_actions(_inc(Status.TRIGGERED), "anna") == []


def test_email_html_has_buttons_not_raw_links():
    email = FakeEmail()
    d = MultiDispatcher("https://dz.example", None, email, link_secret=SECRET)
    contact = Contact(user_id="anna", name="Анна", email="anna@example.com")
    inc = _inc(Status.TRIGGERED)
    inc.title = "Диск <b>полон</b>"
    asyncio.run(d.page(contact, inc, "API", "Новый инцидент"))
    html = email.sent[0][3]
    import re

    buttons = re.findall(r'<a href="(https://dz\.example/a/[^"]+)"[^>]*>([^<]+)</a>', html)
    assert [label for _, label in buttons] == ["Беру", "Решено"]
    # адреса только в href, текстом ссылок не видно
    visible = re.sub(r"<[^>]+>", "", html)
    assert "/a/" not in visible and "https://" not in visible
    assert "Диск &lt;b&gt;полон&lt;/b&gt;" in html  # заголовок экранирован
    assert ">Открыть в панели</a>" in html

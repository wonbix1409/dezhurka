import base64

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from tests.conftest import CONFIG

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin123").decode()}
KEY = "api-key-0123456789"


@pytest.fixture
def client(tmp_path):
    cfg = dict(CONFIG)
    cfg["settings"] = {**CONFIG["settings"], "database_url": f"sqlite+aiosqlite:///{tmp_path}/api.db"}
    app = create_app(Config.model_validate(cfg), start_background=False)
    with TestClient(app) as c:
        yield c


def test_ingest_alertmanager_and_dashboard(client):
    body = {"alerts": [{"status": "firing", "labels": {"alertname": "Down", "instance": "web-1"}, "fingerprint": "f1"}]}
    r = client.post(f"/api/v1/ingest/{KEY}/alertmanager", json=body)
    assert r.status_code == 200 and r.json()["results"][0]["action"] == "created"
    iid = r.json()["results"][0]["incident_id"]

    assert client.get("/").status_code == 401
    page = client.get("/", headers=AUTH)
    assert page.status_code == 200 and "Down (web-1)" in page.text

    detail = client.get(f"/incidents/{iid}", headers=AUTH)
    assert detail.status_code == 200 and "Хронология" in detail.text

    r = client.post(f"/incidents/{iid}/ack", data={"actor": "petr"}, headers=AUTH, follow_redirects=False)
    assert r.status_code == 303
    assert "Пётр" in client.get(f"/incidents/{iid}", headers=AUTH).text

    r = client.post(f"/api/v1/incidents/{iid}/resolve", params={"actor": "CI"}, headers=AUTH)
    assert r.json()["status"] == "resolved"
    assert client.post(f"/api/v1/incidents/{iid}/resolve", headers=AUTH).status_code == 409


def test_ingest_errors(client):
    assert client.post("/api/v1/ingest/wrong-key-000000/generic", json={"title": "x"}).status_code == 404
    assert client.post(f"/api/v1/ingest/{KEY}/nagios", json={"title": "x"}).status_code == 404
    assert client.post(f"/api/v1/ingest/{KEY}/generic", json={"nope": 1}).status_code == 422
    assert client.post(f"/api/v1/ingest/{KEY}/generic", content=b"not json").status_code == 400
    assert client.post(f"/api/v1/ingest/{KEY}/generic", json=[1, 2]).status_code == 422


def test_csrf_and_overrides(client):
    r = client.post(
        "/schedules/primary/overrides",
        data={"user_id": "petr", "start": "2026-11-04T09:00", "end": "2026-11-04T21:00"},
        headers={**AUTH, "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    r = client.post(
        "/schedules/primary/overrides",
        data={"user_id": "petr", "start": "2026-11-04T21:00", "end": "2026-11-04T09:00"},
        headers=AUTH,
        follow_redirects=False,
    )
    assert "error" in r.headers["location"]
    page = client.get("/schedules", headers=AUTH)
    assert page.status_code == 200 and "Подмены" in page.text
    assert client.get("/people", headers=AUTH).status_code == 200

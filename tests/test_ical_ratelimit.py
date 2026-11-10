from __future__ import annotations

from datetime import datetime

from dezhurka import ical
from dezhurka.config import Config
from dezhurka.ratelimit import RateLimiter
from dezhurka.schedule import DbOverride

from .conftest import CONFIG


def test_shifts_split_by_override():
    cfg = Config.model_validate(CONFIG)
    sch = cfg.schedule("primary")
    start = datetime(2026, 11, 4, 0, 0)
    end = datetime(2026, 11, 6, 0, 0)
    ov = [DbOverride("lead", datetime(2026, 11, 4, 20, 0), datetime(2026, 11, 5, 2, 0))]
    got = ical.shifts(sch, start, end, ov)
    assert [s.user_id for s in got if s.is_override] == ["lead"]
    # смены идут встык, без дыр
    assert all(a.end == b.start for a, b in zip(got, got[1:]))
    assert got[0].start == start and got[-1].end == end


def test_ics_has_events_and_escapes():
    cfg = Config.model_validate(CONFIG)
    text = ical.render(cfg, cfg.schedule("primary"), [], datetime(2026, 11, 4, 12, 0), days=10)
    assert text.startswith("BEGIN:VCALENDAR") and text.count("BEGIN:VEVENT") >= 10
    assert "SUMMARY:Иван" in text or "SUMMARY:Пётр" in text


def test_rate_limit_window():
    rl = RateLimiter(3)
    assert [rl.allow("k", t) for t in (0, 1, 2, 3)] == [True, True, True, False]
    assert rl.allow("k", 61)  # окно сдвинулось
    assert RateLimiter(0).allow("x")


def test_ics_endpoint_requires_token(tmp_path):
    from fastapi.testclient import TestClient

    from dezhurka.main import create_app

    cfg = Config.model_validate({**CONFIG, "settings": {**CONFIG["settings"], "database_url": f"sqlite+aiosqlite:///{tmp_path}/i.db"}})
    with TestClient(create_app(cfg, start_background=False)) as c:
        ok = ical.token(cfg.settings.secret_key, "primary")
        assert c.get(f"/ical/primary/{ok}.ics").headers["content-type"].startswith("text/calendar")
        assert c.get("/ical/primary/wrong.ics").status_code == 404

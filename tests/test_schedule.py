from datetime import datetime

from app.schedule import DbOverride, local_to_utc, on_call, upcoming


def test_rotation_by_day(config):
    s = config.schedule("primary")
    # Ротация стартует 2 ноября в 10:00 МСК = 07:00 UTC, смена сутки
    assert on_call(s, datetime(2026, 11, 2, 7, 0)).user_id == "ivan"
    assert on_call(s, datetime(2026, 11, 3, 6, 59)).user_id == "ivan"
    assert on_call(s, datetime(2026, 11, 3, 7, 0)).user_id == "petr"
    assert on_call(s, datetime(2026, 11, 4, 12, 0)).user_id == "anna"
    assert on_call(s, datetime(2026, 11, 5, 7, 0)).user_id == "ivan"  # круг замкнулся


def test_before_rotation_start_wraps(config):
    s = config.schedule("primary")
    # За сутки до старта — последний в списке
    assert on_call(s, datetime(2026, 11, 1, 8, 0)).user_id == "anna"


def test_override_wins_and_latest_wins(config):
    s = config.schedule("primary")
    at = datetime(2026, 11, 4, 12, 0)
    o1 = DbOverride("ivan", datetime(2026, 11, 4, 10), datetime(2026, 11, 4, 14))
    o2 = DbOverride("petr", datetime(2026, 11, 4, 11), datetime(2026, 11, 4, 13))
    shift = on_call(s, at, [o1, o2])
    assert shift.user_id == "petr" and shift.is_override
    assert on_call(s, datetime(2026, 11, 4, 13, 30), [o1, o2]).user_id == "ivan"
    assert on_call(s, datetime(2026, 11, 4, 14, 0), [o1, o2]).user_id == "anna"


def test_upcoming(config):
    s = config.schedule("primary")
    shifts = upcoming(s, datetime(2026, 11, 4, 12, 0), count=3)
    assert [x.user_id for x in shifts] == ["anna", "ivan", "petr"]
    assert shifts[0].start == local_to_utc(datetime(2026, 11, 4, 10, 0), "Europe/Moscow")

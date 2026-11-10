import pytest

from dezhurka.engine import ActionError
from dezhurka.ingest import RESOLVE, TRIGGER, Alert
from dezhurka.models import Incident, ScheduleOverride, Status


def alert(action=TRIGGER, key="k1", title="API лежит"):
    return Alert(action=action, dedup_key=key, title=title)


async def get(eng, incident_id):
    async with eng.sessions() as s:
        return await s.get(Incident, incident_id)


def test_new_incident_pages_on_call_and_team(env):
    eng, disp, clock, run = env
    svc = eng.config.service("api")
    [res] = run(eng.ingest(svc, [alert()]))
    assert res.action == "created"
    # 4 ноября дежурит Анна; плюс пост в общий чат команды
    assert disp.pages == [("anna", res.incident_id, "Новый инцидент")]
    assert disp.team_posts == [(-500, res.incident_id)]
    inc = run(get(eng, res.incident_id))
    assert inc.status == Status.TRIGGERED and inc.paged_users == ["anna"]


def test_dedup_does_not_page_again(env):
    eng, disp, clock, run = env
    svc = eng.config.service("api")
    first = run(eng.ingest(svc, [alert()]))[0]
    second = run(eng.ingest(svc, [alert()]))[0]
    assert second.action == "deduplicated" and second.incident_id == first.incident_id
    assert len(disp.pages) == 1
    assert run(get(eng, first.incident_id)).alert_count == 2


def test_escalation_steps_loop_and_exhaust(env):
    eng, disp, clock, run = env
    svc = eng.config.service("api")
    iid = run(eng.ingest(svc, [alert()]))[0].incident_id

    clock.advance(minutes=4)
    assert run(eng.tick()) == 0  # ещё не время
    clock.advance(minutes=1)
    assert run(eng.tick()) == 1  # шаг 2 — тимлид
    assert disp.pages[-1][0] == "lead"
    clock.advance(minutes=10)
    run(eng.tick())  # repeat=1 — второй круг, снова дежурный
    assert disp.pages[-1] == ("anna", iid, "Эскалация: шаг 1, круг 2")
    clock.advance(minutes=5)
    run(eng.tick())
    assert disp.pages[-1][0] == "lead"
    clock.advance(minutes=10)
    assert run(eng.tick()) == 0  # круги кончились
    inc = run(get(eng, iid))
    assert inc.next_escalation_at is None and inc.status == Status.TRIGGERED
    assert len(disp.pages) == 4


def test_ack_stops_escalation_and_notifies(env):
    eng, disp, clock, run = env
    svc = eng.config.service("api")
    iid = run(eng.ingest(svc, [alert()]))[0].incident_id
    run(eng.acknowledge(iid, "Анна"))
    clock.advance(minutes=30)
    assert run(eng.tick()) == 0
    assert len(disp.pages) == 1
    inc_id, status, messages, contacts = disp.status_changes[-1]
    assert status == Status.ACKNOWLEDGED and contacts == ["anna"]
    assert len(messages) == 1  # сообщение в чате команды (у Анны нет Telegram)
    with pytest.raises(ActionError):
        run(eng.acknowledge(iid, "Пётр"))


def test_source_resolve_closes_and_new_alert_opens_new_incident(env):
    eng, disp, clock, run = env
    svc = eng.config.service("api")
    iid = run(eng.ingest(svc, [alert()]))[0].incident_id
    res = run(eng.ingest(svc, [alert(action=RESOLVE)]))[0]
    assert res.action == "resolved" and res.incident_id == iid
    assert run(get(eng, iid)).status == Status.RESOLVED
    assert run(eng.ingest(svc, [alert(action=RESOLVE)]))[0].action == "ignored"
    again = run(eng.ingest(svc, [alert()]))[0]
    assert again.action == "created" and again.incident_id != iid


def test_db_override_changes_who_is_paged(env):
    eng, disp, clock, run = env
    from datetime import timedelta

    async def add_override():
        async with eng.sessions() as s:
            s.add(ScheduleOverride(schedule_id="primary", user_id="petr", start=clock() - timedelta(hours=1), end=clock() + timedelta(hours=1)))
            await s.commit()

    run(add_override())
    run(eng.ingest(eng.config.service("api"), [alert()]))
    assert disp.pages[0][0] == "petr"


def test_manual_resolve(env):
    eng, disp, clock, run = env
    iid = run(eng.ingest(eng.config.service("api"), [alert()]))[0].incident_id
    run(eng.resolve(iid, "Пётр"))
    inc = run(get(eng, iid))
    assert inc.status == Status.RESOLVED and inc.resolved_by == "Пётр"
    with pytest.raises(ActionError):
        run(eng.resolve(iid, "Пётр"))

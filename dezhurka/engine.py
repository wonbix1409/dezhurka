"""Ядро: инциденты, дедупликация, подтверждение, эскалации."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from .config import Config, EscalationStep, Service
from .ingest import RESOLVE, Alert
from .models import Incident, IncidentEvent, ScheduleOverride, SentMessage, Status, TelegramLink, utcnow
from .notify.base import Contact, Dispatcher
from .schedule import DbOverride, on_call

log = logging.getLogger("dezhurka.engine")


@dataclass
class IngestResult:
    incident_id: int
    action: str  # created | deduplicated | resolved | ignored


class ActionError(Exception):
    pass


class Engine:
    def __init__(
        self,
        config: Config,
        sessions: async_sessionmaker,
        dispatcher: Dispatcher,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.dispatcher = dispatcher
        self.clock = clock
        # Один процесс, одна очередь изменений: просто и без гонок на SQLite
        self._lock = asyncio.Lock()

    # ---------- вспомогательное ----------

    async def _overrides(self, session: AsyncSession, schedule_id: str) -> list[DbOverride]:
        rows = (
            await session.scalars(
                select(ScheduleOverride).where(ScheduleOverride.schedule_id == schedule_id).order_by(ScheduleOverride.id)
            )
        ).all()
        return [DbOverride(r.user_id, r.start, r.end) for r in rows]

    async def who_is_on_call(self, session: AsyncSession, schedule_id: str, at: datetime | None = None):
        schedule = self.config.schedule(schedule_id)
        if schedule is None:
            return None
        return on_call(schedule, at or self.clock(), await self._overrides(session, schedule_id))

    async def contact(self, session: AsyncSession, user_id: str) -> Contact | None:
        user = self.config.user(user_id)
        if user is None:
            return None
        chat_id = user.telegram_chat_id
        if chat_id is None:
            link = await session.get(TelegramLink, user_id)
            chat_id = link.chat_id if link else None
        return Contact(user.id, user.name, user.email, chat_id)

    async def _step_users(self, session: AsyncSession, step: EscalationStep, now: datetime) -> list[str]:
        users: list[str] = []
        for t in step.targets:
            if t.user:
                uid = t.user
            else:
                shift = await self.who_is_on_call(session, t.schedule, now)
                uid = shift.user_id if shift else None
            if uid and uid not in users:
                users.append(uid)
        return users

    def _event(self, incident: Incident, kind: str, message: str = "", actor: str | None = None) -> None:
        incident.events.append(IncidentEvent(kind=kind, message=message, actor=actor, at=self.clock()))

    async def _load(self, session: AsyncSession, incident_id: int) -> Incident | None:
        return await session.scalar(
            select(Incident)
            .where(Incident.id == incident_id)
            .options(selectinload(Incident.events), selectinload(Incident.messages))
        )

    # ---------- приём алертов ----------

    async def ingest(self, service: Service, alerts: list[Alert]) -> list[IngestResult]:
        results: list[IngestResult] = []
        async with self._lock:
            for alert in alerts:
                results.append(await self._ingest_one(service, alert))
        return results

    async def _ingest_one(self, service: Service, alert: Alert) -> IngestResult:
        now = self.clock()
        to_page: Incident | None = None
        changed: Incident | None = None
        async with self.sessions() as session:
            existing = await session.scalar(
                select(Incident)
                .where(
                    Incident.service_id == service.id,
                    Incident.dedup_key == alert.dedup_key,
                    Incident.status.in_(Status.OPEN),
                )
                .options(selectinload(Incident.events), selectinload(Incident.messages))
                .order_by(Incident.id.desc())
                .limit(1)
            )
            if alert.action == RESOLVE:
                if existing is None:
                    return IngestResult(0, "ignored")
                self._close(existing, now, actor=f"{alert.source} (авто)")
                self._event(existing, "auto_resolved", "Источник прислал «решено»")
                await session.commit()
                changed = existing
                result = IngestResult(existing.id, "resolved")
            elif existing is not None:
                # Тот же алерт снова — не будим людей повторно, только считаем
                existing.alert_count += 1
                existing.last_alert_at = now
                await session.commit()
                return IngestResult(existing.id, "deduplicated")
            else:
                incident = Incident(
                    service_id=service.id,
                    dedup_key=alert.dedup_key,
                    title=alert.title,
                    details=alert.details,
                    severity=alert.severity,
                    source=alert.source,
                    labels=alert.labels,
                    created_at=now,
                    last_alert_at=now,
                    events=[],
                    messages=[],
                )
                session.add(incident)
                self._event(incident, "created", f"Алерт из {alert.source}")
                await session.commit()
                to_page = incident
                result = IngestResult(incident.id, "created")

        if to_page is not None:
            await self._start_escalation(to_page.id, service)
        if changed is not None:
            await self._broadcast_status(changed.id)
        return result

    # ---------- эскалации ----------

    async def _start_escalation(self, incident_id: int, service: Service) -> None:
        if service.team_chat_id:
            async with self.sessions() as session:
                incident = await self._load(session, incident_id)
                deliveries = await self.dispatcher.team_post(service.team_chat_id, incident, service.name)
                for d in deliveries:
                    if d.ok and d.chat_id and d.message_id:
                        incident.messages.append(SentMessage(chat_id=d.chat_id, message_id=d.message_id))
                await session.commit()
        await self._run_step(incident_id, step_index=0, loop=0)

    async def _run_step(self, incident_id: int, step_index: int, loop: int) -> None:
        now = self.clock()
        async with self.sessions() as session:
            incident = await self._load(session, incident_id)
            if incident is None or incident.status != Status.TRIGGERED:
                return
            service = self.config.service(incident.service_id)
            policy = self.config.policy(service.escalation_policy) if service else None
            if service is None or policy is None:
                self._event(incident, "error", "Сервис или политика удалены из конфига")
                incident.next_escalation_at = None
                await session.commit()
                return
            step = policy.steps[step_index]
            user_ids = await self._step_users(session, step, now)
            reason = (
                "Новый инцидент"
                if step_index == 0 and loop == 0
                else f"Эскалация: шаг {step_index + 1}" + (f", круг {loop + 1}" if loop else "")
            )
            names = []
            paged = list(incident.paged_users or [])
            for uid in user_ids:
                contact = await self.contact(session, uid)
                if contact is None:
                    continue
                names.append(contact.name)
                if uid not in paged:
                    paged.append(uid)
                try:
                    deliveries = await self.dispatcher.page(contact, incident, service.name, reason)
                except Exception as e:  # канал не должен ломать эскалацию
                    log.exception("ошибка отправки %s", uid)
                    deliveries = []
                    self._event(incident, "delivery_failed", f"{contact.name}: {e}")
                for d in deliveries:
                    if d.ok and d.chat_id and d.message_id:
                        incident.messages.append(SentMessage(chat_id=d.chat_id, message_id=d.message_id))
                    if not d.ok:
                        self._event(incident, "delivery_failed", f"{contact.name}, {d.channel}: {d.error}")
                if not any(d.ok for d in deliveries):
                    self._event(
                        incident, "unreachable", f"{contact.name}: нет ни одного рабочего канала связи"
                    )
            kind = "notified" if step_index == 0 and loop == 0 else "escalated"
            self._event(incident, kind, f"{reason}. Позвали: {', '.join(names) or 'никого (никто не найден)'}")
            incident.paged_users = paged
            incident.escalation_step = step_index
            incident.escalation_loop = loop
            incident.next_escalation_at = now + timedelta(minutes=step.escalate_after_min)
            await session.commit()

    async def tick(self) -> int:
        """Пройтись по инцидентам, у которых подошло время эскалации. Возвращает число шагов."""
        now = self.clock()
        done = 0
        async with self._lock:
            async with self.sessions() as session:
                due = (
                    await session.scalars(
                        select(Incident).where(
                            Incident.status == Status.TRIGGERED,
                            Incident.next_escalation_at.is_not(None),
                            Incident.next_escalation_at <= now,
                        )
                    )
                ).all()
                plan: list[tuple[int, int, int]] = []
                for inc in due:
                    service = self.config.service(inc.service_id)
                    policy = self.config.policy(service.escalation_policy) if service else None
                    if policy is None:
                        inc.next_escalation_at = None
                        continue
                    nxt, loop = inc.escalation_step + 1, inc.escalation_loop
                    if nxt >= len(policy.steps):
                        if loop < policy.repeat:
                            nxt, loop = 0, loop + 1
                        else:
                            inc.next_escalation_at = None
                            await session.refresh(inc, ["events"])
                            self._event(inc, "exhausted", "Эскалация исчерпана, инцидент никто не взял")
                            continue
                    plan.append((inc.id, nxt, loop))
                await session.commit()
            for incident_id, nxt, loop in plan:
                await self._run_step(incident_id, nxt, loop)
                done += 1
        return done

    # ---------- действия людей ----------

    def _close(self, incident: Incident, now: datetime, actor: str) -> None:
        incident.status = Status.RESOLVED
        incident.resolved_at = now
        incident.resolved_by = actor
        incident.next_escalation_at = None

    async def acknowledge(self, incident_id: int, actor: str) -> Incident:
        async with self._lock:
            async with self.sessions() as session:
                incident = await self._load(session, incident_id)
                if incident is None:
                    raise ActionError("Инцидент не найден")
                if incident.status == Status.RESOLVED:
                    raise ActionError("Инцидент уже решён")
                if incident.status == Status.ACKNOWLEDGED:
                    raise ActionError(f"Уже взял: {incident.acknowledged_by}")
                incident.status = Status.ACKNOWLEDGED
                incident.acknowledged_at = self.clock()
                incident.acknowledged_by = actor
                incident.next_escalation_at = None
                self._event(incident, "acknowledged", "Взял в работу", actor)
                await session.commit()
            await self._broadcast_status(incident_id)
            return incident

    async def resolve(self, incident_id: int, actor: str) -> Incident:
        async with self._lock:
            async with self.sessions() as session:
                incident = await self._load(session, incident_id)
                if incident is None:
                    raise ActionError("Инцидент не найден")
                if incident.status == Status.RESOLVED:
                    raise ActionError("Инцидент уже решён")
                self._close(incident, self.clock(), actor)
                self._event(incident, "resolved", "Отметил решённым", actor)
                await session.commit()
            await self._broadcast_status(incident_id)
            return incident

    async def _broadcast_status(self, incident_id: int) -> None:
        async with self.sessions() as session:
            incident = await self._load(session, incident_id)
            service = self.config.service(incident.service_id)
            # Сообщаем всем, кого успели позвать по этому инциденту
            contacts = []
            for uid in incident.paged_users or []:
                c = await self.contact(session, uid)
                if c:
                    contacts.append(c)
            messages = [(m.chat_id, m.message_id) for m in incident.messages]
            try:
                await self.dispatcher.status_changed(
                    incident, service.name if service else incident.service_id, messages, contacts
                )
            except Exception:  # уведомление не должно ронять смену статуса
                log.exception("не удалось разослать смену статуса инцидента %s", incident_id)

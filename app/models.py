"""Модели БД. Все времена хранятся как naive UTC."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Status:
    TRIGGERED = "triggered"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    OPEN = (TRIGGERED, ACKNOWLEDGED)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[str] = mapped_column(String(64), index=True)
    dedup_key: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(500))
    details: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(32), default="critical")
    source: Mapped[str] = mapped_column(String(32), default="generic")
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=Status.TRIGGERED, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_alert_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    alert_count: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Состояние эскалации
    escalation_step: Mapped[int] = mapped_column(Integer, default=-1)  # последний выполненный шаг
    escalation_loop: Mapped[int] = mapped_column(Integer, default=0)
    next_escalation_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    paged_users: Mapped[list] = mapped_column(JSON, default=list)  # кого уже звали

    events: Mapped[list["IncidentEvent"]] = relationship(
        back_populates="incident", order_by="IncidentEvent.id", cascade="all, delete-orphan"
    )
    messages: Mapped[list["SentMessage"]] = relationship(cascade="all, delete-orphan")


class IncidentEvent(Base):
    """Таймлайн инцидента: что произошло, кто и когда."""

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")

    incident: Mapped[Incident] = relationship(back_populates="events")


class SentMessage(Base):
    """Отправленные сообщения в Telegram — чтобы обновлять их при смене статуса."""

    __tablename__ = "sent_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)


class TelegramLink(Base):
    """Привязка пользователя из конфига к его личному чату с ботом."""

    __tablename__ = "telegram_links"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduleOverride(Base):
    """Подмены дежурств, добавленные через панель/API без правки конфига."""

    __tablename__ = "schedule_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    end: Mapped[datetime] = mapped_column(DateTime)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KV(Base):
    """Мелкое служебное состояние (например, offset Telegram)."""

    __tablename__ = "kv"
    __table_args__ = (UniqueConstraint("key"),)

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

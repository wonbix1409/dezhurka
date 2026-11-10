"""Telegram-бот: привязка аккаунтов, кнопки «Беру/Решено», команды /oncall и /open.

Работает через long polling — серверу не нужен публичный HTTPS, что удобно
для установки внутри закрытого контура.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from datetime import datetime
from html import escape

from sqlalchemy import select

from .engine import ActionError, Engine
from .models import KV, Incident, Status, TelegramLink, utcnow
from .notify.telegram import TelegramAPI
from .notify.text import STATUS, fmt_time

log = logging.getLogger("dezhurka.bot")

_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")


def _redact(e: BaseException) -> str:
    """Текст ошибки без токена бота (он бывает в URL Bot API)."""
    return _TOKEN_RE.sub("<TOKEN>", f"{type(e).__name__}: {e}")

HELP = (
    "Я бот дежурств «Дежурка».\n\n"
    "/oncall — кто сейчас дежурит\n"
    "/open — открытые инциденты\n"
    "/whoami — к кому привязан этот аккаунт\n\n"
    "Кнопки под инцидентами: «Беру» — взять в работу и остановить эскалацию, «Решено» — закрыть."
)


def link_code(secret: str, user_id: str) -> str:
    """Код для ссылки t.me/<bot>?start=<code>. Без таблиц: проверяется пересчётом HMAC."""
    return hmac.new(secret.encode(), f"link:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


class Bot:
    def __init__(self, engine: Engine, api: TelegramAPI) -> None:
        self.engine = engine
        self.api = api
        self.username: str | None = None
        self._stop = asyncio.Event()
        # С какого момента Telegram недоступен (naive UTC) и последняя ошибка — для предупреждений
        self.failing_since: datetime | None = None
        self.last_error: str = ""

    def _failed(self, error: str) -> None:
        if self.failing_since is None:
            self.failing_since = utcnow()
        self.last_error = error

    def _ok(self) -> None:
        if self.failing_since is not None:
            log.info("Связь с Telegram восстановлена")
        self.failing_since = None
        self.last_error = ""

    @property
    def config(self):
        return self.engine.config

    def deep_link(self, user_id: str) -> str | None:
        if not self.username:
            return None
        return f"https://t.me/{self.username}?start={link_code(self.config.settings.secret_key, user_id)}"

    async def actor_for(self, tg_user_id: int) -> tuple[str | None, str | None]:
        """(user_id, имя) привязанного пользователя по Telegram id."""
        async with self.engine.sessions() as s:
            link = await s.scalar(select(TelegramLink).where(TelegramLink.tg_user_id == tg_user_id))
        if link:
            user = self.config.user(link.user_id)
            return link.user_id, user.name if user else link.user_id
        for u in self.config.users:  # chat_id, заданный прямо в конфиге (личка = id пользователя)
            if u.telegram_chat_id == tg_user_id:
                return u.id, u.name
        return None, None

    # ---------- обработчики ----------

    async def on_start(self, msg: dict, arg: str) -> None:
        chat = msg["chat"]
        sender = msg["from"]
        if chat.get("type") != "private":
            await self.reply(msg, f"chat_id этого чата: <code>{chat['id']}</code> — укажите его в team_chat_id сервиса.")
            return
        if not arg:
            await self.reply(msg, HELP)
            return
        user = next(
            (u for u in self.config.users if hmac.compare_digest(link_code(self.config.settings.secret_key, u.id), arg)),
            None,
        )
        if user is None:
            await self.reply(msg, "Ссылка недействительна. Возьмите свежую в панели «Дежурки».")
            return
        async with self.engine.sessions() as s:
            # Один Telegram — один пользователь: снимаем старые привязки
            for old in (
                await s.scalars(
                    select(TelegramLink).where(
                        (TelegramLink.tg_user_id == sender["id"]) | (TelegramLink.user_id == user.id)
                    )
                )
            ).all():
                await s.delete(old)
            await s.flush()
            s.add(
                TelegramLink(user_id=user.id, chat_id=chat["id"], tg_user_id=sender["id"], tg_username=sender.get("username"))
            )
            await s.commit()
        await self.reply(msg, f"Готово, {escape(user.name)}! Теперь инциденты будут приходить сюда.\n\n{HELP}")

    async def on_oncall(self, msg: dict) -> None:
        lines = []
        async with self.engine.sessions() as s:
            for sch in self.config.schedules:
                shift = await self.engine.who_is_on_call(s, sch.id)
                user = self.config.user(shift.user_id)
                tail = " (подмена)" if shift.is_override else ""
                lines.append(
                    f"<b>{escape(sch.name or sch.id)}</b>: {escape(user.name if user else shift.user_id)}{tail}, "
                    f"до {fmt_time(shift.end, sch.timezone)}"
                )
        await self.reply(msg, "\n".join(lines) or "Графики не настроены.")

    async def on_open(self, msg: dict) -> None:
        async with self.engine.sessions() as s:
            rows = (
                await s.scalars(
                    select(Incident).where(Incident.status.in_(Status.OPEN)).order_by(Incident.id.desc()).limit(15)
                )
            ).all()
        if not rows:
            await self.reply(msg, "Открытых инцидентов нет 🎉")
            return
        lines = [f"#{i.id} {STATUS[i.status]} — {escape(i.title)}" for i in rows]
        await self.reply(msg, "\n".join(lines))

    async def on_whoami(self, msg: dict) -> None:
        uid, name = await self.actor_for(msg["from"]["id"])
        await self.reply(msg, f"Вы: {escape(name)} ({uid})" if uid else "Аккаунт не привязан. Возьмите ссылку в панели.")

    async def on_callback(self, cb: dict) -> None:
        data = cb.get("data", "")
        action, _, raw_id = data.partition(":")
        if action not in {"ack", "res"} or not raw_id.isdigit():
            await self.api.call("answerCallbackQuery", callback_query_id=cb["id"])
            return
        uid, name = await self.actor_for(cb["from"]["id"])
        if uid is None:
            await self.api.call(
                "answerCallbackQuery",
                callback_query_id=cb["id"],
                text="Сначала привяжите Telegram к «Дежурке» по ссылке из панели.",
                show_alert=True,
            )
            return
        try:
            if action == "ack":
                await self.engine.acknowledge(int(raw_id), name)
                text = "Вы взяли инцидент. Эскалация остановлена."
            else:
                await self.engine.resolve(int(raw_id), name)
                text = "Инцидент закрыт."
        except ActionError as e:
            text = str(e)
        await self.api.call("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    async def allowed(self, msg: dict) -> bool:
        """Сведения об инцидентах — только своим: привязанным людям и чатам команд из конфига."""
        if msg["chat"].get("type") == "private":
            uid, _ = await self.actor_for(msg["from"]["id"])
            return uid is not None
        return any(s.team_chat_id == msg["chat"]["id"] for s in self.config.services)

    async def reply(self, msg: dict, text: str) -> None:
        await self.api.call(
            "sendMessage",
            chat_id=msg["chat"]["id"],
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )

    async def handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg or "text" not in msg:
            return
        cmd, _, arg = msg["text"].strip().partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        if cmd == "/start":
            await self.on_start(msg, arg.strip())
            return
        if cmd in {"/oncall", "/open"} and not await self.allowed(msg):
            await self.reply(msg, "Нет доступа. Привяжите аккаунт по ссылке из панели «Дежурки».")
            return
        if cmd == "/oncall":
            await self.on_oncall(msg)
        elif cmd == "/open":
            await self.on_open(msg)
        elif cmd == "/whoami":
            await self.on_whoami(msg)
        elif cmd == "/help" and msg["chat"].get("type") == "private":
            await self.reply(msg, HELP)

    # ---------- цикл ----------

    async def _get_offset(self) -> int:
        async with self.engine.sessions() as s:
            row = await s.get(KV, "tg_offset")
            return int(row.value) if row else 0

    async def _set_offset(self, offset: int) -> None:
        async with self.engine.sessions() as s:
            row = await s.get(KV, "tg_offset")
            if row:
                row.value = str(offset)
            else:
                s.add(KV(key="tg_offset", value=str(offset)))
            await s.commit()

    async def _connect(self) -> bool:
        """getMe + команды; при сбое повторяем с паузой до 5 минут, пока не остановят."""
        delay = 5
        while not self._stop.is_set():
            try:
                me = await self.api.call("getMe")
                self.username = me.get("username")
                await self.api.call(
                    "setMyCommands",
                    commands=[
                        {"command": "oncall", "description": "Кто сейчас дежурит"},
                        {"command": "open", "description": "Открытые инциденты"},
                        {"command": "whoami", "description": "Привязка аккаунта"},
                    ],
                )
                log.info("Telegram-бот @%s запущен", self.username)
                self._ok()
                return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._failed(_redact(e))
                log.error(
                    "Не удалось подключиться к Telegram (%s) — проверьте bot_token и доступ к api.telegram.org; "
                    "повтор через %s с",
                    _redact(e), delay,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 300)
        return False

    async def run(self) -> None:
        if not await self._connect():
            return
        offset = await self._get_offset()
        backoff = 1
        while not self._stop.is_set():
            try:
                updates = await self.api.call(
                    "getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"]
                )
                backoff = 1
                self._ok()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._failed(_redact(e))
                log.warning("getUpdates: %s; повтор через %s с", _redact(e), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    await self.handle(upd)
                except Exception:
                    log.exception("ошибка обработки апдейта %s", upd.get("update_id"))
            if updates:
                await self._set_offset(offset)

    def stop(self) -> None:
        self._stop.set()

"""Бот с поддельным Telegram API: привязка, права, кнопки."""
import json

import httpx

from app.bot import Bot, link_code
from app.ingest import Alert
from app.models import Status
from app.notify.telegram import TelegramAPI


class FakeTG:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        result = {"message_id": len(self.calls)} if method == "sendMessage" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    def texts(self):
        return [b.get("text", "") for m, b in self.calls if m in {"sendMessage", "answerCallbackQuery"}]


def make_bot(eng):
    fake = FakeTG()
    api = TelegramAPI("TEST", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return Bot(eng, api), fake


def msg(text, user_id=555, chat_id=555, chat_type="private"):
    return {"message": {"text": text, "from": {"id": user_id, "username": "anna_tg"}, "chat": {"id": chat_id, "type": chat_type}}}


def cb(data, user_id=555):
    return {"callback_query": {"id": "q1", "data": data, "from": {"id": user_id}}}


def test_link_and_ack_via_button(env):
    eng, disp, clock, run = env
    bot, fake = make_bot(eng)
    iid = run(eng.ingest(eng.config.service("api"), [Alert("trigger", "k", "БД недоступна")]))[0].incident_id

    # Чужой не может нажимать кнопки и смотреть инциденты
    run(bot.handle(cb(f"ack:{iid}")))
    assert "привяжите" in fake.texts()[-1]
    run(bot.handle(msg("/open")))
    assert "Нет доступа" in fake.texts()[-1]

    # Неверный код не привязывает
    run(bot.handle(msg("/start deadbeef")))
    assert "недействительна" in fake.texts()[-1]

    code = link_code(eng.config.settings.secret_key, "anna")
    run(bot.handle(msg(f"/start {code}")))
    assert "Готово, Анна" in fake.texts()[-1]

    run(bot.handle(msg("/open")))
    assert "БД недоступна" in fake.texts()[-1]
    run(bot.handle(msg("/oncall")))
    assert "Анна" in fake.texts()[-1]

    run(bot.handle(cb(f"ack:{iid}")))
    assert "взяли" in fake.texts()[-1]
    run(bot.handle(cb(f"ack:{iid}")))
    assert "Уже взял" in fake.texts()[-1]
    run(bot.handle(cb(f"res:{iid}")))

    async def status():
        from app.models import Incident

        async with eng.sessions() as s:
            return (await s.get(Incident, iid)).status

    assert run(status()) == Status.RESOLVED


def test_group_chat_start_shows_chat_id(env):
    eng, disp, clock, run = env
    bot, fake = make_bot(eng)
    run(bot.handle(msg("/start", chat_id=-777, chat_type="group")))
    assert "-777" in fake.texts()[-1]
    # Чат команды из конфига (-500) может смотреть /oncall
    run(bot.handle(msg("/oncall", user_id=1, chat_id=-500, chat_type="supergroup")))
    assert "Анна" in fake.texts()[-1]


def test_real_dispatcher_sends_and_edits(env):
    from app.notify.dispatcher import MultiDispatcher

    eng, _, clock, run = env
    fake = FakeTG()
    api = TelegramAPI("TEST", client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    eng.dispatcher = MultiDispatcher("https://dezhurka.local", api, None)
    eng.config.schedule("primary").rotation.users[:] = ["ivan", "petr", "ivan"]  # сегодня дежурит Иван (есть Telegram)
    iid = run(eng.ingest(eng.config.service("api"), [Alert("trigger", "k", "Очередь <jobs> растёт", details="lag > 10k")]))[0].incident_id

    sends = [b for m, b in fake.calls if m == "sendMessage"]
    assert {b["chat_id"] for b in sends} == {-500, 101}  # чат команды + личка дежурного
    assert "&lt;jobs&gt;" in sends[0]["text"]  # HTML экранирован
    assert sends[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"ack:{iid}"

    run(eng.acknowledge(iid, "Иван"))
    edits = [b for m, b in fake.calls if m == "editMessageText"]
    assert len(edits) == 2 and all("В работе: Иван" in e["text"] for e in edits)
    assert edits[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"res:{iid}"

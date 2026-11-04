"""Точка входа: HTTP API для вебхуков, веб-панель и фоновые задачи."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .bot import Bot
from .config import Config, load_config
from .db import init_db, make_engine, make_sessionmaker
from .engine import ActionError, Engine
from .ingest import PARSERS, IngestError
from .models import Incident, ScheduleOverride, Status, TelegramLink, utcnow
from .notify.dispatcher import MultiDispatcher
from .notify.email import EmailSender
from .notify.telegram import TelegramAPI
from .notify.text import SEVERITY, STATUS, fmt_time
from .schedule import local_to_utc, upcoming

log = logging.getLogger("dezhurka")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MAX_BODY = 1_000_000  # 1 МБ на вебхук


async def escalation_loop(engine: Engine, every: float) -> None:
    while True:
        try:
            await engine.tick()
        except Exception:
            log.exception("ошибка в цикле эскалаций")
        await asyncio.sleep(every)


def create_app(config: Config | None = None, start_background: bool = True) -> FastAPI:
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = make_engine(config.settings.database_url)
        await init_db(db)
        telegram = (
            TelegramAPI(config.telegram.bot_token) if config.telegram.enabled and config.telegram.bot_token else None
        )
        dispatcher = MultiDispatcher(config.settings.public_url, telegram, EmailSender(config.smtp))
        engine = Engine(config, make_sessionmaker(db), dispatcher)
        app.state.engine = engine
        app.state.bot = Bot(engine, telegram) if telegram else None
        tasks = []
        if start_background:
            tasks.append(asyncio.create_task(escalation_loop(engine, config.settings.tick_seconds)))
            if app.state.bot:
                tasks.append(asyncio.create_task(app.state.bot.run()))
        if not telegram:
            log.warning("Telegram выключен: не задан bot_token")
        if not dispatcher.email:
            log.warning("Почта выключена: не задан smtp.host/sender")
        yield
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if telegram:
            await telegram.close()
        await db.dispose()

    app = FastAPI(title="Дежурка", version="0.1.0", lifespan=lifespan, docs_url="/api/docs")
    security = HTTPBasic(realm="Dezhurka")
    tz = config.settings.timezone

    def admin(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
        ok = secrets.compare_digest(creds.password.encode(), config.settings.admin_password.encode())
        if not ok:
            raise HTTPException(401, "Неверный пароль", headers={"WWW-Authenticate": 'Basic realm="Dezhurka"'})
        # Защита форм от CSRF: браузер сам шлёт Basic-авторизацию на любые запросы к нам
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "Запрос с чужого сайта")
        return creds.username or "admin"

    def engine(request: Request) -> Engine:
        return request.app.state.engine

    def render(request: Request, name: str, **ctx):
        ctx.update(request=request, STATUS=STATUS, SEVERITY=SEVERITY, fmt=lambda d, z=tz: fmt_time(d, z), config=config)
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # ---------- приём алертов ----------

    @app.post("/api/v1/ingest/{integration_key}/{kind}")
    async def ingest(integration_key: str, kind: str, request: Request, eng: Engine = Depends(engine)):
        service = config.service_by_key(integration_key)
        if service is None:
            raise HTTPException(404, "Неизвестный ключ интеграции")
        parser = PARSERS.get(kind)
        if parser is None:
            raise HTTPException(404, f"Неизвестный тип интеграции; доступны: {', '.join(PARSERS)}")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Слишком большой запрос")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise IngestError("ожидался JSON-объект")
            alerts = parser(payload)
        except IngestError as e:
            raise HTTPException(422, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, "Тело запроса — не JSON") from e
        results = await eng.ingest(service, alerts)
        return {"results": [r.__dict__ for r in results]}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    # ---------- REST для действий ----------

    @app.post("/api/v1/incidents/{incident_id}/{action}")
    async def api_action(incident_id: int, action: str, actor: str = "API", _=Depends(admin), eng: Engine = Depends(engine)):
        try:
            if action == "acknowledge":
                inc = await eng.acknowledge(incident_id, actor)
            elif action == "resolve":
                inc = await eng.resolve(incident_id, actor)
            else:
                raise HTTPException(404, "Действия: acknowledge, resolve")
        except ActionError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"id": inc.id, "status": inc.status}

    # ---------- веб-панель ----------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _=Depends(admin), eng: Engine = Depends(engine)):
        async with eng.sessions() as s:
            open_incidents = (
                await s.scalars(select(Incident).where(Incident.status.in_(Status.OPEN)).order_by(Incident.id.desc()))
            ).all()
            recent = (
                await s.scalars(
                    select(Incident).where(Incident.status == Status.RESOLVED).order_by(Incident.id.desc()).limit(20)
                )
            ).all()
            on_call = []
            for sch in config.schedules:
                shift = await eng.who_is_on_call(s, sch.id)
                on_call.append((sch, shift, config.user(shift.user_id)))
            week_ago = utcnow() - timedelta(days=7)
            week = await s.scalar(select(func.count()).select_from(Incident).where(Incident.created_at >= week_ago))
            acked = (
                await s.execute(
                    select(Incident.created_at, Incident.acknowledged_at).where(
                        Incident.created_at >= week_ago, Incident.acknowledged_at.is_not(None)
                    )
                )
            ).all()
        # MTTA — среднее время до реакции дежурного за неделю
        if acked:
            minutes = sum((a - c).total_seconds() for c, a in acked) / len(acked) / 60
            mtta = f"{minutes:.0f} мин" if minutes >= 1 else "< 1 мин"
        else:
            mtta = "—"
        return render(request, "index.html", open_incidents=open_incidents, recent=recent, on_call=on_call,
                      week=week, mtta=mtta)

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    async def incident_page(incident_id: int, request: Request, _=Depends(admin), eng: Engine = Depends(engine)):
        async with eng.sessions() as s:
            inc = await s.scalar(
                select(Incident).where(Incident.id == incident_id).options(selectinload(Incident.events))
            )
        if inc is None:
            raise HTTPException(404, "Инцидент не найден")
        service = config.service(inc.service_id)
        return render(request, "incident.html", inc=inc, service=service, error=request.query_params.get("error"))

    @app.post("/incidents/{incident_id}/{action}")
    async def incident_action(
        incident_id: int, action: str, actor: str = Form(...), _=Depends(admin), eng: Engine = Depends(engine)
    ):
        user = config.user(actor)
        name = user.name if user else "Панель"
        try:
            if action == "ack":
                await eng.acknowledge(incident_id, name)
            elif action == "resolve":
                await eng.resolve(incident_id, name)
            else:
                raise HTTPException(404)
        except ActionError as e:
            return RedirectResponse(f"/incidents/{incident_id}?error={e}", status_code=303)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    @app.get("/people", response_class=HTMLResponse)
    async def people(request: Request, _=Depends(admin), eng: Engine = Depends(engine)):
        async with eng.sessions() as s:
            links = {l.user_id: l for l in (await s.scalars(select(TelegramLink))).all()}
        bot: Bot | None = request.app.state.bot
        rows = [
            (u, links.get(u.id), bot.deep_link(u.id) if bot else None)
            for u in config.users
        ]
        return render(request, "people.html", rows=rows, bot=bot)

    @app.get("/schedules", response_class=HTMLResponse)
    async def schedules(request: Request, _=Depends(admin), eng: Engine = Depends(engine)):
        now = utcnow()
        data = []
        async with eng.sessions() as s:
            for sch in config.schedules:
                current = await eng.who_is_on_call(s, sch.id, now)
                overrides = (
                    await s.scalars(
                        select(ScheduleOverride)
                        .where(ScheduleOverride.schedule_id == sch.id, ScheduleOverride.end > now)
                        .order_by(ScheduleOverride.start)
                    )
                ).all()
                data.append((sch, current, upcoming(sch, now, 6), overrides))
        return render(request, "schedules.html", data=data, error=request.query_params.get("error"))

    @app.post("/schedules/{schedule_id}/overrides")
    async def add_override(
        schedule_id: str,
        user_id: str = Form(...),
        start: str = Form(...),
        end: str = Form(...),
        _=Depends(admin),
        eng: Engine = Depends(engine),
    ):
        sch = config.schedule(schedule_id)
        if sch is None or config.user(user_id) is None:
            raise HTTPException(404)
        try:
            s_utc = local_to_utc(datetime.fromisoformat(start), sch.timezone)
            e_utc = local_to_utc(datetime.fromisoformat(end), sch.timezone)
        except ValueError:
            return RedirectResponse("/schedules?error=Неверный формат даты", status_code=303)
        if e_utc <= s_utc:
            return RedirectResponse("/schedules?error=Конец подмены должен быть позже начала", status_code=303)
        async with eng.sessions() as s:
            s.add(ScheduleOverride(schedule_id=schedule_id, user_id=user_id, start=s_utc, end=e_utc))
            await s.commit()
        return RedirectResponse("/schedules", status_code=303)

    @app.post("/schedules/{schedule_id}/overrides/{override_id}/delete")
    async def delete_override(schedule_id: str, override_id: int, _=Depends(admin), eng: Engine = Depends(engine)):
        async with eng.sessions() as s:
            row = await s.get(ScheduleOverride, override_id)
            if row and row.schedule_id == schedule_id:
                await s.delete(row)
                await s.commit()
        return RedirectResponse("/schedules", status_code=303)

    return app


def app_factory() -> FastAPI:  # для uvicorn --factory
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет полный URL запроса, а в URL Bot API лежит токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return create_app()

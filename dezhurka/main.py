"""Точка входа: HTTP API для вебхуков, веб-панель и фоновые задачи."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .action_links import LinkError, read_token
from .auth import BasicPasswordAuth, require_principal
from .bot import Bot
from .config import Config, load_config
from .db import init_db, make_engine, make_sessionmaker
from .engine import ActionError, Engine
from .extensions import Extension, load_extensions, migration_sets, run_setup, run_shutdown, run_startup
from . import ical
from .health import ChannelWatchdog, watchdog_loop
from .ingest import PARSERS, IngestError
from .models import Incident, ScheduleOverride, Status, TelegramLink, utcnow
from .notify.dispatcher import MultiDispatcher
from .notify.email import EmailSender
from .notify.telegram import TelegramAPI
from .notify.text import SEVERITY, STATUS, fmt_time
from .ratelimit import RateLimiter
from .schedule import DbOverride, local_to_utc, upcoming

log = logging.getLogger("dezhurka")
CORE_TEMPLATES = Path(__file__).parent / "templates"
MAX_BODY = 1_000_000  # 1 МБ на вебхук


async def escalation_loop(engine: Engine, every: float) -> None:
    while True:
        try:
            await engine.tick()
        except Exception:
            log.exception("ошибка в цикле эскалаций")
        await asyncio.sleep(every)


def create_app(
    config: Config | None = None,
    start_background: bool = True,
    extensions: Sequence[Extension] = (),
) -> FastAPI:
    config = config or load_config()
    exts = list(extensions)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = make_engine(config.settings.database_url)
        await init_db(db, migration_sets(exts))
        app.state.db = db
        telegram = (
            TelegramAPI(config.telegram.bot_token, proxy=config.telegram.proxy or None)
            if config.telegram.enabled and config.telegram.bot_token
            else None
        )
        dispatcher = MultiDispatcher(
            config.settings.public_url, telegram, EmailSender(config.smtp), link_secret=config.settings.secret_key
        )
        engine = Engine(config, make_sessionmaker(db), dispatcher)
        app.state.dispatcher = dispatcher
        app.state.engine = engine
        app.state.bot = Bot(engine, telegram) if telegram else None
        app.state.watchdog = ChannelWatchdog(config, app.state.bot, dispatcher.email)
        tasks = []
        if start_background:
            tasks.append(asyncio.create_task(escalation_loop(engine, config.settings.tick_seconds)))
            tasks.append(asyncio.create_task(watchdog_loop(app.state.watchdog, utcnow)))
            if app.state.bot:
                tasks.append(asyncio.create_task(app.state.bot.run()))
        if not telegram:
            log.warning("Telegram выключен: не задан bot_token")
        if not dispatcher.email:
            log.warning("Почта выключена: не задан smtp.host/sender")
        await run_startup(exts, app)
        yield
        await run_shutdown(exts, app)
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if telegram:
            await telegram.close()
        await db.dispose()

    app = FastAPI(title="Дежурка", version="0.2.0", lifespan=lifespan, docs_url="/api/docs")
    app.state.config = config
    app.state.extensions = exts
    # Расширение может заменить бэкенд авторизации в своём setup()
    app.state.auth = BasicPasswordAuth(config)
    app.state.nav = []  # пункты меню от расширений: {"href", "label", "roles"?}
    app.state.logout_url = None
    app.state.incident_links = []  # ссылки на странице инцидента: {"href": "/x/{id}", "label"}
    tz = config.settings.timezone

    # Расширения могут принести свои шаблоны (templates_dir) — они видят шаблоны ядра
    templates = Jinja2Templates(directory=str(CORE_TEMPLATES))
    ext_dirs = [str(d) for e in exts if (d := getattr(e, "templates_dir", None))]
    templates.env.loader = ChoiceLoader([FileSystemLoader(str(CORE_TEMPLATES)), *map(FileSystemLoader, ext_dirs)])
    limiter = RateLimiter(config.settings.ingest_rate_per_min)

    admin = require_principal

    def acting_name(principal, form_actor: str = "") -> str:
        """Под чьим именем записать действие: у личной учётки — её владелец, при общем пароле — выбранный в форме."""
        if principal.shared:
            user = config.user(form_actor)
            return user.name if user else "Панель"
        user = config.user(principal.user_id) if principal.user_id else None
        return user.name if user else principal.name

    def engine(request: Request) -> Engine:
        return request.app.state.engine

    def _nav_visible(item: dict, principal) -> bool:
        roles = item.get("roles")
        return not roles or (principal is not None and bool(set(roles) & set(principal.roles)))

    def render(request: Request, name: str, **ctx):
        ctx.update(
            request=request,
            STATUS=STATUS,
            SEVERITY=SEVERITY,
            fmt=lambda d, z=tz: fmt_time(d, z),
            config=config,
            principal=getattr(request.state, "principal", None),
            nav_items=[n for n in request.app.state.nav if _nav_visible(n, getattr(request.state, "principal", None))],
            logout_url=request.app.state.logout_url,
            incident_links=request.app.state.incident_links,
        )
        watchdog = getattr(request.app.state, "watchdog", None)
        # предупреждения о каналах — только в закрытой части панели
        ctx.setdefault("channel_warnings", watchdog.warnings(utcnow()) if watchdog and ctx["principal"] else [])
        return templates.TemplateResponse(request, name, ctx)

    app.state.render = render

    # ---------- приём алертов ----------

    @app.post("/api/v1/ingest/{integration_key}/{kind}")
    async def ingest(integration_key: str, kind: str, request: Request, eng: Engine = Depends(engine)):
        service = config.service_by_key(integration_key)
        if service is None:
            raise HTTPException(404, "Неизвестный ключ интеграции")
        parser = PARSERS.get(kind)
        if parser is None:
            raise HTTPException(404, f"Неизвестный тип интеграции; доступны: {', '.join(PARSERS)}")
        if not limiter.allow(integration_key):
            raise HTTPException(429, "Слишком много запросов", headers={"Retry-After": "60"})
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
    async def api_action(incident_id: int, action: str, actor: str = "API", principal=Depends(admin), eng: Engine = Depends(engine)):
        actor = actor if principal.shared else acting_name(principal)
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
        incident_id: int, action: str, actor: str = Form(""), principal=Depends(admin), eng: Engine = Depends(engine)
    ):
        name = acting_name(principal, actor)
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
        base = config.settings.public_url.rstrip("/")
        ics = {sch.id: f"{base}/ical/{sch.id}/{ical.token(config.settings.secret_key, sch.id)}.ics" for sch in config.schedules}
        return render(request, "schedules.html", data=data, ics=ics, error=request.query_params.get("error"))

    @app.get("/ical/{schedule_id}/{key}.ics")
    async def schedule_ics(schedule_id: str, key: str, eng: Engine = Depends(engine)):
        # без входа в панель: календарные клиенты не умеют логиниться, доступ — по секретной ссылке
        sch = config.schedule(schedule_id)
        if sch is None or not hmac.compare_digest(key, ical.token(config.settings.secret_key, schedule_id)):
            raise HTTPException(404)
        async with eng.sessions() as s:
            rows = (await s.scalars(select(ScheduleOverride).where(ScheduleOverride.schedule_id == schedule_id))).all()
        body = ical.render(config, sch, [DbOverride(r.user_id, r.start, r.end) for r in rows], utcnow())
        return Response(body, media_type="text/calendar; charset=utf-8", headers={"Cache-Control": "no-store"})

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

    # ---------- ссылки из писем: «Беру» / «Решено» без входа в панель ----------

    ACTION_LABELS = {"ack": ("Беру", "Взято в работу"), "resolve": ("Решено", "Инцидент закрыт")}

    async def _claim(token: str, eng: Engine):
        claim = read_token(config.settings.secret_key, token, utcnow())
        user = config.user(claim.user_id)
        if user is None:
            raise LinkError("этого человека больше нет в конфиге")
        async with eng.sessions() as s:
            inc = await s.get(Incident, claim.incident_id)
        if inc is None:
            raise LinkError("инцидент не найден")
        return claim, user, inc

    def _action_page(request: Request, status_code: int = 200, **ctx):
        resp = render(request, "action.html", title="Дежурка", **ctx)
        resp.status_code = status_code
        # ссылка-ключ не должна утекать в Referer и кэши
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/a/{token}", response_class=HTMLResponse)
    async def action_link(token: str, request: Request, eng: Engine = Depends(engine)):
        # GET ничего не меняет: почтовые сканеры открывают ссылки сами
        try:
            claim, user, inc = await _claim(token, eng)
        except LinkError as e:
            return _action_page(request, 400, error=str(e))
        button, _ = ACTION_LABELS[claim.action]
        return _action_page(
            request, inc=inc, service=config.service(inc.service_id), button=button, user_name=user.name
        )

    @app.post("/a/{token}", response_class=HTMLResponse)
    async def action_link_do(token: str, request: Request, eng: Engine = Depends(engine)):
        try:
            claim, user, inc = await _claim(token, eng)
            if claim.action == "ack":
                inc = await eng.acknowledge(claim.incident_id, user.name)
            else:
                inc = await eng.resolve(claim.incident_id, user.name)
        except (LinkError, ActionError) as e:
            return _action_page(request, 400 if isinstance(e, LinkError) else 409, error=str(e))
        log.info("Инцидент #%s: «%s» по ссылке из письма (%s)", inc.id, claim.action, user.id)
        return _action_page(request, inc=inc, done=ACTION_LABELS[claim.action][1])

    # Маршруты и замены от расширений — после маршрутов ядра
    run_setup(exts, app, config)
    return app


def app_factory() -> FastAPI:  # для uvicorn --factory
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет полный URL запроса, а в URL Bot API лежит токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return create_app(extensions=load_extensions())

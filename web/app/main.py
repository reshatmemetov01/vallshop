"""VallShop публичный сайт — точка входа FastAPI.

Отдельный сервис (свой контейнер/деплой), общая с ботом БД и бизнес-логика.
Запуск: uvicorn web.app.main:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request

from bot.db import models as _models  # noqa: F401 — регистрация моделей (web_accounts)
from bot.db.base import Base
from bot.db.models import SiteVisit

from .config import settings
from .db import database
from .migrate import ensure_source_columns, ensure_web_accounts_nullable
from .routers import activate, admin, auth, buy, catalog, orders, pages, reviews, topup


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создаём ТОЛЬКО недостающие таблицы (в т.ч. web_accounts). НЕ запускаем
    # мигратор колонок бота — схемой владеет бот, а два процесса на общей БД
    # не должны мигрировать одновременно. create_all не трогает существующие.
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Довести web_accounts до nullable email/password_hash (для аккаунтов
        # через Telegram без email) — если таблица создана старой схемой.
        await conn.run_sync(ensure_web_accounts_nullable)
        # orders.source / topups.source — для раздельной статистики бот/сайт.
        await conn.run_sync(ensure_source_columns)
    yield


app = FastAPI(title="VallShop Web API", version="0.1.0", lifespan=lifespan)


# Маркеры ботов/краулеров/скриптов в User-Agent (нижний регистр, подстрока).
# Имена ботов обычно содержат "bot" (googlebot, yandexbot, telegrambot…) —
# ловим его отдельно, кроме телефона Cubot (в его UA тоже есть 'bot').
_BOT_MARKERS = (
    "crawl", "spider", "slurp", "mediapartners", "facebookexternalhit", "facebot",
    "embedly", "whatsapp", "telegram", "discord", "slack", "vkshare",
    "skypeuripreview", "linkedin", "redditbot", "pinterest", "tumblr", "nuzzel",
    "applebot", "duckduck", "baiduspider", "petal", "bytespider", "ahrefs",
    "semrush", "mj12", "dotbot", "dataforseo", "blexbot", "seznam", "archive.org",
    "ia_archiver", "python-", "go-http", "okhttp", "java/", "libwww", "scrapy",
    "httpclient", "aiohttp", "node-fetch", "axios", "curl/", "wget", "headless",
    "phantom", "puppeteer", "playwright", "uptimerobot", "pingdom", "statuscake",
    "site24x7", "monitoring", "+http", "google-inspectiontool", "feedfetcher",
)


def _is_bot(ua: str) -> bool:
    u = (ua or "").lower()
    if not u:
        return True  # пустой User-Agent — почти всегда скрипт/бот
    probe = u.replace("cubot", "")  # Cubot — это телефон, а не бот
    if "bot" in probe:
        return True
    return any(m in u for m in _BOT_MARKERS)


@app.middleware("http")
async def _count_visits(request: Request, call_next):
    """Считать посещения живых людей (не боты, не владелец). Лучший-эффорт."""
    response = await call_next(request)
    try:
        if request.method == "GET" and response.status_code == 200:
            ct = response.headers.get("content-type", "")
            path = request.url.path
            if ("text/html" in ct
                    and not path.startswith("/admin")
                    and path != "/notrack"
                    and not request.cookies.get("vs_notrack")      # исключить владельца
                    and not _is_bot(request.headers.get("user-agent", ""))):
                await _log_visit(request)
    except Exception:  # noqa: BLE001 — статистика не должна ломать ответ
        pass
    return response


async def _log_visit(request: Request) -> None:
    from sqlalchemy import select, update
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "")
    if ip and ip in settings.owner_ips:
        return  # заходы владельца (по IP) не считаем
    ua = request.headers.get("user-agent", "")
    visitor = hashlib.sha256(f"{ip}|{ua}|{settings.jwt_secret}".encode()).hexdigest()[:32]
    day = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")  # МСК
    async with database.session() as s:
        existing = await s.scalar(
            select(SiteVisit).where(SiteVisit.day == day, SiteVisit.visitor == visitor)
        )
        if existing:
            await s.execute(
                update(SiteVisit).where(SiteVisit.id == existing.id)
                .values(hits=SiteVisit.hits + 1, updated_at=datetime.utcnow())
            )
        else:
            s.add(SiteVisit(day=day, visitor=visitor, hits=1))
        await s.commit()


app.include_router(catalog.router)
app.include_router(auth.router)
app.include_router(topup.router)
app.include_router(buy.router)
app.include_router(activate.router)
app.include_router(admin.router)
app.include_router(orders.router)
app.include_router(reviews.router)
app.include_router(pages.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "vallshop-web", "version": app.version}

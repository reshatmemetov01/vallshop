"""VallShop публичный сайт — точка входа FastAPI.

Отдельный сервис (свой контейнер/деплой), общая с ботом БД и бизнес-логика.
Запуск: uvicorn web.app.main:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .db import database
from .routers import auth, catalog, pages


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Гарантируем, что таблицы (в т.ч. web_accounts) существуют в общей БД.
    await database.create_all()
    yield


app = FastAPI(title="VallShop Web API", version="0.1.0", lifespan=lifespan)
app.include_router(catalog.router)
app.include_router(auth.router)
app.include_router(pages.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "vallshop-web", "version": app.version}

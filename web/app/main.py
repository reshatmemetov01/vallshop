"""VallShop публичный сайт — точка входа FastAPI.

Отдельный сервис (свой контейнер/деплой), общая с ботом БД и бизнес-логика.
Запуск: uvicorn web.app.main:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

from fastapi import FastAPI

from .config import settings
from .routers import catalog, pages

app = FastAPI(title="VallShop Web API", version="0.1.0")
app.include_router(catalog.router)
app.include_router(pages.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "vallshop-web", "version": app.version}

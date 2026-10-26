"""Подключение к общей БД (переиспользуем движок/сессии бота)."""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.base import Database  # общий слой БД с ботом

from .config import settings

database = Database(settings.database_url)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: сессия БД на запрос."""
    async with database.session() as session:
        yield session

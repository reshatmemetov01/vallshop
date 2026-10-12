"""Мидлвари бота."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .db.models import User
from .services.users import get_user


class BlockedMiddleware(BaseMiddleware):
    """Отсекает апдейты от заблокированных пользователей (кроме админов)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        config = data.get("config")
        db = data.get("db")
        user = data.get("event_from_user")
        if user and config and db and not config.is_admin(user.id):
            async with db.session() as s:
                u = await get_user(s, user.id)
                if u is not None and u.is_blocked:
                    return None  # игнорируем
        return await handler(event, data)

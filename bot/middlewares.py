"""Мидлвари бота."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .db.models import HEAD_MERCHANT_ID
from .services.users import get_user


class MerchantMiddleware(BaseMiddleware):
    """Определяет мерчанта по боту, в который пришёл апдейт, и кладёт
    ``merchant_id`` в данные хендлера (головной бот → 0)."""

    def __init__(self, bot_to_merchant: Dict[int, int]) -> None:
        self._map = dict(bot_to_merchant)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        bot = data.get("bot")
        mid = HEAD_MERCHANT_ID
        if bot is not None:
            mid = self._map.get(bot.id, HEAD_MERCHANT_ID)
        data["merchant_id"] = mid
        return await handler(event, data)


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
        merchant_id = data.get("merchant_id", HEAD_MERCHANT_ID)
        if user and config and db and not config.is_admin(user.id):
            async with db.session() as s:
                u = await get_user(s, user.id, merchant_id)
                if u is not None:
                    if u.is_blocked:
                        return None  # игнорируем (ручной бан админа)
                    # Человек снова пишет боту → снова достижим для рассылки.
                    if not u.is_active:
                        u.is_active = True
                        await s.commit()
        return await handler(event, data)

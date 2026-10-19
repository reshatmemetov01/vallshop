"""Мидлвари бота."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

import logging

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from .db.models import HEAD_MERCHANT_ID
from .services.users import get_user

log = logging.getLogger("vallshop.mw")


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


class ClickTrackingMiddleware(BaseMiddleware):
    """Учёт кликов по кнопкам поста рассылки.

    Кнопка-раздел в посте имеет callback ``bck:<broadcast_id>:<idx>:<target>``.
    Middleware (внешний, для callback_query) записывает клик и переписывает
    data в ``target``, чтобы дальше сработал обычный хендлер раздела — навигация
    не ломается.
    """

    async def __call__(self, handler, event, data):
        d = getattr(event, "data", None)
        if isinstance(d, str) and d.startswith("bck:"):
            target = "menu"  # безопасный фолбэк, если разбор не удался
            try:
                _, bid, idx, target = d.split(":", 3)
                db = data.get("db")
                user = data.get("event_from_user")
                if db is not None and user is not None:
                    from .services import broadcasts as broadcasts_service
                    await broadcasts_service.record_click(db, int(bid), int(idx), user.id)
            except Exception:  # noqa: BLE001 — учёт клика не должен ломать навигацию
                log.exception("Не удалось учесть клик рассылки (data=%s)", d)
            # Переписываем событие на реальный раздел (модели aiogram неизменяемы).
            event = event.model_copy(update={"data": target})
        return await handler(event, data)


class ChannelGateMiddleware(BaseMiddleware):
    """Обязательная подписка на канал: не подписан → блокируем и просим подписаться.

    Работает только когда в магазине включена обязательная подписка и привязан
    канал. Админы/владельцы магазина не блокируются. Если проверить нельзя
    (бот не админ канала / ошибка) — пускаем (fail-open). Колбэк «chksub»
    (кнопка «Проверить подписку») пропускаем, чтобы его обработчик отработал.
    """

    async def __call__(self, handler, event, data):
        from .services import channel as channel_service
        from .services import merchants as merchants_service
        from . import keyboards as kb

        user = data.get("event_from_user")
        bot = data.get("bot")
        config = data.get("config")
        db = data.get("db")
        merchant_id = data.get("merchant_id", HEAD_MERCHANT_ID)
        if not (user and bot and db):
            return await handler(event, data)

        cfg = channel_service.config(merchant_id)
        if not cfg.get("required") or not cfg.get("chat"):
            return await handler(event, data)  # гейт выключен

        # Колбэк проверки подписки и клики по кнопкам поста в канале — пропускаем
        # (их обработчики сами разберутся; на канальные посты гейт не распространяем).
        ev_data = getattr(event, "data", None)
        if ev_data == "chksub" or (isinstance(ev_data, str) and ev_data.startswith("cpk:")):
            return await handler(event, data)

        # Админы/владельцы магазина не блокируются.
        if config is not None and config.is_admin(user.id):
            return await handler(event, data)
        if await merchants_service.admin_merchant(db, user.id, merchant_id) is not None:
            return await handler(event, data)

        sub = await channel_service.is_subscribed(bot, merchant_id, user.id)
        if sub is False:
            await _send_gate(event, cfg, kb)
            return None  # блокируем действие
        return await handler(event, data)


async def _send_gate(event, cfg: dict, kb) -> None:
    """Показать экран «подпишитесь на канал» (сообщение или ответ на колбэк)."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    url = cfg.get("url") or channel_url_from_chat(cfg.get("chat"))
    b = InlineKeyboardBuilder()
    if url:
        b.row(kb._b("📢 Перейти в канал", url=url))
    b.row(kb._b("✅ Проверить подписку", callback_data="chksub"))
    text = ("🔒 Чтобы пользоваться ботом, подпишитесь на наш канал.\n"
            "После подписки нажмите «✅ Проверить подписку».")
    try:
        if hasattr(event, "message") and event.message is not None:  # CallbackQuery
            await event.answer()
            await event.message.answer(text, reply_markup=b.as_markup())
        else:  # Message
            await event.answer(text, reply_markup=b.as_markup())
    except Exception:  # noqa: BLE001
        pass


def channel_url_from_chat(chat) -> Optional[str]:
    """Для @username-канала собрать ссылку t.me, иначе None."""
    if isinstance(chat, str) and chat.startswith("@"):
        return "https://t.me/" + chat[1:]
    return None


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

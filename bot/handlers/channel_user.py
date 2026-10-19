"""Пользовательская часть обязательной подписки: кнопка «Проверить подписку».

Работает во всех ботах (merchant_id из middleware). Экран-гейт показывает
ChannelGateMiddleware; здесь — повторная (свежая) проверка по кнопке.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import channel as channel_service
from ..services import menu as menu_service
from ..services import merchants as merchants_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router(name="channel_user")


@router.callback_query(F.data == "chksub")
async def cb_check_sub(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    sub = await channel_service.is_subscribed(call.bot, merchant_id, call.from_user.id, force=True)
    if sub is False:
        await call.answer("Вы ещё не подписались на канал 🙁", show_alert=True)
        return
    # Подписан (или проверить нельзя → fail-open): открываем доступ, показываем меню.
    await call.answer("Спасибо! Доступ открыт ✅")
    is_admin = config.is_admin(call.from_user.id)
    is_madmin = await merchants_service.admin_merchant(db, call.from_user.id, merchant_id) is not None
    await render(
        call, banner="main",
        caption=menu_service.caption("main", texts.START, merchant_id),
        reply_markup=kb.main_menu_kb(is_admin, is_madmin, merchant_id),
    )

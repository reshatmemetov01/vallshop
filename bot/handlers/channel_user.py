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
from ..services import broadcasts as broadcasts_service
from ..services import menu as menu_service
from ..services import merchants as merchants_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router(name="channel_user")


@router.callback_query(F.data.startswith("cpk:"))
async def cb_channel_post_click(
    call: CallbackQuery, db: Database, merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    """Клик по кнопке поста в канале: засчитать и открыть бота по deep-link."""
    parts = call.data.split(":")  # cpk:<post_id>:<idx>:<target>
    try:
        pid, idx = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        await call.answer()
        return
    target = parts[3] if len(parts) > 3 else ""
    await broadcasts_service.record_click(db, pid, idx, call.from_user.id)
    try:
        me = await call.bot.me()
        payload = f"s{target}" if target else "open"
        await call.answer(url=f"https://t.me/{me.username}?start={payload}")
    except Exception:  # noqa: BLE001
        await call.answer("Открываю бота…")


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

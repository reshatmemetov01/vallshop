"""Админ-раздел «Мерчанты»: список франшиз, статус, удаление (у владельца)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..services import merchants as merchants_service

router = Router(name="admin_merchants")


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


router.callback_query.filter(IsAdminCb())


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _show_list(call: CallbackQuery, db: Database) -> None:
    merchants = await merchants_service.list_all(db)
    kb = InlineKeyboardBuilder()
    if not merchants:
        caption = "🏪 <b>Мерчанты</b>\n\nФраншиз пока нет."
    else:
        caption = f"🏪 <b>Мерчанты</b>\n\nВсего франшиз: <b>{len(merchants)}</b>\nВыберите для управления:"
        for m in merchants:
            mark = "🟢" if m.status == "active" else "⏸"
            kb.row(_btn(f"{mark} @{m.bot_username or m.id}", f"mch:{m.id}"))
    kb.row(_btn("⬅️ Назад", "rf_home"))
    try:
        await call.message.edit_text(caption, reply_markup=kb.as_markup())
    except Exception:  # noqa: BLE001
        await call.message.answer(caption, reply_markup=kb.as_markup())


async def _show_detail(call: CallbackQuery, db: Database, mid: int) -> None:
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    st = await merchants_service.merchant_stats(db, mid)
    status_ru = "🟢 активна" if m.status == "active" else "⏸ приостановлена"
    caption = (
        f"🏪 <b>Франшиза #{m.id}</b>\n\n"
        f"Бот: @{m.bot_username or '—'}\n"
        f"Админ: @{m.admin_username or '—'} (id <code>{m.admin_user_id}</code>)\n"
        f"Статус: {status_ru}\n\n"
        f"Клиентов: <b>{st['users']}</b>\n"
        f"Заказов: <b>{st['orders']}</b>"
    )
    kb = InlineKeyboardBuilder()
    if m.status == "active":
        kb.row(_btn("⏸ Приостановить", f"mch_tog:{m.id}"))
    else:
        kb.row(_btn("🟢 Активировать", f"mch_tog:{m.id}"))
    kb.row(_btn("🗑 Удалить франшизу", f"mch_del:{m.id}"))
    kb.row(_btn("⬅️ К списку", "mch_home"))
    await call.message.edit_text(caption, reply_markup=kb.as_markup())


@router.callback_query(F.data == "mch_home")
async def cb_list(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_list(call, db)
    await call.answer()


@router.callback_query(F.data.startswith("mch:"))
async def cb_detail(call: CallbackQuery, db: Database) -> None:
    await _show_detail(call, db, int(call.data.split(":", 1)[1]))
    await call.answer()


@router.callback_query(F.data.startswith("mch_tog:"))
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await merchants_service.set_status(db, mid, "suspended" if m.status == "active" else "active")
    await _show_detail(call, db, mid)
    await call.answer("Готово. Вступит в силу после перезапуска сервиса.", show_alert=True)


@router.callback_query(F.data.startswith("mch_del:"))
async def cb_delete_ask(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить со всеми данными", f"mch_delok:{mid}"))
    kb.row(_btn("⬅️ Отмена", f"mch:{mid}"))
    await call.message.edit_text(
        f"🗑 Удалить франшизу <b>@{m.bot_username or m.id}</b>?\n\n"
        "Будут удалены ВСЕ её данные: клиенты, балансы, заказы, пополнения, отзывы.\n"
        "Это необратимо. Бот перестанет работать после перезапуска сервиса.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("mch_delok:"))
async def cb_delete(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    ok = await merchants_service.delete_merchant(db, mid)
    await call.answer("Франшиза удалена ✅" if ok else "Не удалось", show_alert=True)
    await _show_list(call, db)

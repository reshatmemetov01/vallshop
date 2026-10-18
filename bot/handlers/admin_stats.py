"""Раздел «📊 Статистика» в админ-панели (только ADMIN_IDS)."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import User
from ..services import balance as balance_service
from ..services.users import get_user
from ..services import stats as stats_service
from .. import texts

router = Router()


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class StatsFlow(StatesGroup):
    custom = State()
    find_user = State()
    credit = State()
    debit = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("Сегодня", "st:today"), _btn("7 дней", "st:7d"))
    kb.row(_btn("30 дней", "st:30d"), _btn("Этот месяц", "st:month"))
    kb.row(_btn("Всё время", "st:all"))
    kb.row(_btn("📅 Свой период", "st_custom"))
    kb.row(_btn("💰 Баланс клиента", "st_bal"))
    kb.row(_btn("⬅️ В админ-панель", "a_home"))
    return kb.as_markup()


def _fmt(label: str, u: dict, s: dict, cur: str, balances=None) -> str:
    from .. import texts
    cost_usd = s.get("cost_usd")
    cost_line = texts.rub(s['cost'])
    if cost_usd:
        cost_line += f" (≈ {texts.money(cost_usd)})"
    bal_line = ""
    if balances is not None:
        bal_line = f"Сумма балансов клиентов: <b>{texts.rub(balances)}</b>\n"
    return (
        f"📊 <b>Статистика</b>\n"
        f"Период: <b>{label}</b>\n\n"
        f"👥 <b>Пользователи</b>\n"
        f"Всего: <b>{u['total']}</b>\n"
        f"Новых за период: <b>{u['new']}</b>\n"
        f"{bal_line}\n"
        f"🛒 <b>Продажи</b>\n"
        f"Заказов: <b>{s['orders']}</b>\n"
        f"Товаров продано: <b>{s['items']}</b>\n"
        f"Выручка: <b>{texts.rub(s['revenue'])}</b>\n"
        f"Себестоимость: {cost_line}\n"
        f"Прибыль: <b>{texts.rub(s['profit'])}</b>"
    )


async def _render(target, db: Database, config: BotConfig, start, end, label, edit=True):
    async with db.session() as session:
        u = await stats_service.users_stats(session, start, end)
        s = await stats_service.sales_stats(session, start, end)
        balances = await stats_service.total_balance(session)
    text = _fmt(label, u, s, config.currency, balances=balances)
    if edit:
        try:
            await target.edit_text(text, reply_markup=_kb())
            return
        except Exception:  # noqa: BLE001
            pass
    await target.answer(text, reply_markup=_kb())


@router.callback_query(F.data == "st_home")
async def cb_stats_home(call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext) -> None:
    await state.clear()
    start, end, label = stats_service.preset_range("today", config.shop_tz)
    await _render(call.message, db, config, start, end, label)
    await call.answer()


@router.callback_query(F.data.startswith("st:"))
async def cb_stats_preset(call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext) -> None:
    await state.clear()
    start, end, label = stats_service.preset_range(call.data.split(":", 1)[1], config.shop_tz)
    await _render(call.message, db, config, start, end, label)
    await call.answer()


@router.callback_query(F.data == "st_custom")
async def cb_stats_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StatsFlow.custom)
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Назад", "st_home"))
    await call.message.edit_text(
        "📅 Пришлите период:\n"
        "• один день — <code>05.10.2026</code>\n"
        "• диапазон — <code>01.10.2026 - 05.10.2026</code>",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.message(StatsFlow.custom)
async def msg_stats_custom(message: Message, db: Database, config: BotConfig, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.replace("—", "-").split("-")]
    try:
        if len(parts) == 1:
            d = datetime.strptime(parts[0], "%d.%m.%Y")
            start, end = stats_service.day_bounds(d, config.shop_tz)
            label = parts[0]
        elif len(parts) == 2:
            d1 = datetime.strptime(parts[0], "%d.%m.%Y")
            d2 = datetime.strptime(parts[1], "%d.%m.%Y")
            start, _ = stats_service.day_bounds(d1, config.shop_tz)
            _, end = stats_service.day_bounds(d2, config.shop_tz)
            label = f"{parts[0]} — {parts[1]}"
        else:
            raise ValueError
    except ValueError:
        await message.answer("Формат: 05.10.2026 или 01.10.2026 - 05.10.2026")
        return
    await state.clear()
    await _render(message, db, config, start, end, label, edit=False)


# ── Баланс клиента ─────────────────────────────────────────────────────────────
async def _find_user(session, raw: str):
    raw = (raw or "").strip().lstrip("@")
    if not raw:
        return None
    if raw.isdigit():
        u = await get_user(session, int(raw))
        if u:
            return u
    return await session.scalar(
        select(User).where(func.lower(User.username) == raw.lower())
    )


async def _bal_card(session, user: User) -> tuple[str, InlineKeyboardMarkup]:
    who = f"@{user.username}" if user.username else "(без username)"
    caption = (
        "💰 <b>Баланс клиента</b>\n\n"
        f"Пользователь: {who}\n"
        f"ID: <code>{user.id}</code>\n"
        f"Баланс: <b>{texts.rub(user.balance)}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("➕ Пополнить", f"bal_add:{user.id}"),
        _btn("➖ Уменьшить", f"bal_sub:{user.id}"),
    )
    kb.row(_btn("🔄 Обновить", f"bal_show:{user.id}"))
    kb.row(_btn("🔎 Другой клиент", "st_bal"), _btn("⬅️ Назад", "st_home"))
    return caption, kb.as_markup()


@router.callback_query(F.data == "st_bal")
async def cb_bal_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StatsFlow.find_user)
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Назад", "st_home"))
    await call.message.edit_text(
        "💰 <b>Баланс клиента</b>\n\n"
        "Пришлите <b>@username</b> или <b>ID</b> пользователя "
        "(свой тоже можно — для теста).",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.message(StatsFlow.find_user)
async def msg_find_user(message: Message, db: Database, state: FSMContext) -> None:
    async with db.session() as session:
        user = await _find_user(session, message.text or "")
        if not user:
            await message.answer("Не найден. Пришлите @username или ID (клиент должен был писать боту).")
            return
        caption, markup = await _bal_card(session, user)
    await state.clear()
    await message.answer(caption, reply_markup=markup)


@router.callback_query(F.data.startswith("bal_show:"))
async def cb_bal_show(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    uid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        user = await get_user(session, uid)
        if not user:
            await call.answer("Не найден", show_alert=True)
            return
        caption, markup = await _bal_card(session, user)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("bal_add:"))
async def cb_bal_add(call: CallbackQuery, state: FSMContext) -> None:
    uid = int(call.data.split(":", 1)[1])
    await state.set_state(StatsFlow.credit)
    await state.update_data(uid=uid)
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Отмена", f"bal_show:{uid}"))
    await call.message.edit_text(
        "➕ Пришлите сумму пополнения в рублях (например <code>500</code>).",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("bal_sub:"))
async def cb_bal_sub(call: CallbackQuery, state: FSMContext) -> None:
    uid = int(call.data.split(":", 1)[1])
    await state.set_state(StatsFlow.debit)
    await state.update_data(uid=uid)
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Отмена", f"bal_show:{uid}"))
    await call.message.edit_text(
        "➖ Пришлите сумму списания в рублях (например <code>100</code>).\n"
        "Если больше баланса — обнулится.",
        reply_markup=b.as_markup(),
    )
    await call.answer()


def _parse_amount(raw: str):
    try:
        v = Decimal((raw or "").strip().replace(",", "."))
        return v if v > 0 else None
    except InvalidOperation:
        return None


@router.message(StatsFlow.credit)
async def msg_bal_credit(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    uid = data.get("uid")
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число, например 500")
        return
    async with db.session() as session:
        user = await get_user(session, uid)
        if not user:
            await state.clear()
            await message.answer("Пользователь не найден")
            return
        await balance_service.credit(session, uid, amount)
        await session.commit()
        user = await get_user(session, uid)
        caption, markup = await _bal_card(session, user)
    await state.clear()
    await message.answer(f"✅ Пополнено на {texts.rub(amount)}")
    await message.answer(caption, reply_markup=markup)


@router.message(StatsFlow.debit)
async def msg_bal_debit(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    uid = data.get("uid")
    amount = _parse_amount(message.text or "")
    if amount is None:
        await message.answer("Введите положительное число, например 100")
        return
    async with db.session() as session:
        user = await get_user(session, uid)
        if not user:
            await state.clear()
            await message.answer("Пользователь не найден")
            return
        cur = Decimal(user.balance or 0)
        take = amount if amount <= cur else cur  # не уходим в минус
        if take > 0:
            await balance_service.try_debit(session, uid, take)
            await session.commit()
        user = await get_user(session, uid)
        caption, markup = await _bal_card(session, user)
    await state.clear()
    await message.answer(f"✅ Списано {texts.rub(take)}")
    await message.answer(caption, reply_markup=markup)

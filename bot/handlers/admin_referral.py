"""Админ-раздел «Рефералы»: настройки программы и статистика."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..services import referral as referral_service
from ..services import settings as settings_service
from .. import texts

router = Router(name="admin_referral")


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class RFState(StatesGroup):
    set_percent = State()
    set_min_usdt = State()
    set_min_rub = State()
    set_terms = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel(back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(_btn("⬅️ Отмена", back)).as_markup()


async def _home(session) -> tuple[str, InlineKeyboardMarkup]:
    enabled = await settings_service.get_bool(session, settings_service.REF_ENABLED, True)
    pct = await settings_service.get(session, settings_service.REF_PERCENT) or str(referral_service.DEFAULT_PERCENT)
    minu = await settings_service.get(session, settings_service.REF_MIN_WD_USDT) or str(referral_service.DEFAULT_MIN_USDT)
    minr = await settings_service.get(session, settings_service.REF_MIN_WD_RUB) or str(referral_service.DEFAULT_MIN_RUB)
    st = await referral_service.admin_stats(session)
    state = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    caption = (
        "👥 <b>Рефералы</b>\n\n"
        f"Статус: <b>{state}</b>\n"
        f"Процент с покупок: <b>{pct}%</b>\n"
        f"Минимум перевода: <b>{minu} USDT</b> / <b>{minr} ₽</b>\n\n"
        "📊 <b>Статистика</b>\n"
        f"Приглашено всего: <b>{st['total_invited']}</b>\n"
        f"Рефоводов с выплатами: <b>{st['total_referrers']}</b>\n"
        f"Выплачено: <b>{texts.rub(st['paid_rub'])}</b>"
        + (f" · <b>{texts.money(st['paid_usdt'])}</b>" if st["paid_usdt"] > 0 else "")
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"Программа: {state} (переключить)", "rf_toggle"))
    kb.row(_btn("✏️ Процент", "rf_pct"), _btn("📝 Условия", "rf_terms"))
    kb.row(_btn("💵 Мин. USDT", "rf_minu"), _btn("💴 Мин. ₽", "rf_minr"))
    kb.row(_btn("🏪 Мерчанты", "mch_home"))
    kb.row(_btn("⬅️ Назад", "a_home"))
    return caption, kb.as_markup()


@router.callback_query(F.data == "rf_home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    async with db.session() as session:
        caption, markup = await _home(session)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "rf_toggle")
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    async with db.session() as session:
        enabled = await settings_service.get_bool(session, settings_service.REF_ENABLED, True)
        await settings_service.set(session, settings_service.REF_ENABLED, "0" if enabled else "1")
        await session.commit()
        caption, markup = await _home(session)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Переключено")


# ── Редактирование числовых настроек ─────────────────────────────────────────
_NUM = {
    "rf_pct": (settings_service.REF_PERCENT, RFState.set_percent, "процент с покупок (например 5)"),
    "rf_minu": (settings_service.REF_MIN_WD_USDT, RFState.set_min_usdt, "минимум перевода в USDT (например 1)"),
    "rf_minr": (settings_service.REF_MIN_WD_RUB, RFState.set_min_rub, "минимум перевода в рублях (например 100)"),
}


@router.callback_query(F.data.in_(set(_NUM.keys())))
async def cb_num_ask(call: CallbackQuery, state: FSMContext) -> None:
    key, st, label = _NUM[call.data]
    await state.set_state(st)
    await call.message.edit_text(
        f"Пришлите <b>{label}</b>.", reply_markup=_cancel("rf_home")
    )
    await call.answer()


async def _save_num(message: Message, db: Database, state: FSMContext, setting_key: str) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        val = Decimal(raw)
        if val < 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("Введите неотрицательное число.")
        return
    async with db.session() as session:
        await settings_service.set(session, setting_key, str(val))
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К рефералам", "rf_home")).as_markup()
    await message.answer("✅ Сохранено", reply_markup=kb)


@router.message(RFState.set_percent)
async def msg_pct(message: Message, db: Database, state: FSMContext) -> None:
    await _save_num(message, db, state, settings_service.REF_PERCENT)


@router.message(RFState.set_min_usdt)
async def msg_minu(message: Message, db: Database, state: FSMContext) -> None:
    await _save_num(message, db, state, settings_service.REF_MIN_WD_USDT)


@router.message(RFState.set_min_rub)
async def msg_minr(message: Message, db: Database, state: FSMContext) -> None:
    await _save_num(message, db, state, settings_service.REF_MIN_WD_RUB)


# ── Текст условий ────────────────────────────────────────────────────────────
@router.callback_query(F.data == "rf_terms")
async def cb_terms_ask(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    async with db.session() as session:
        cur = await referral_service.terms_text(session)
    await state.set_state(RFState.set_terms)
    await call.message.edit_text(
        f"📝 Текущие условия:\n<blockquote>{cur}</blockquote>\n\n"
        "Пришлите новый текст (можно премиум-эмодзи).\n<code>0</code> — по умолчанию.",
        reply_markup=_cancel("rf_home"),
    )
    await call.answer()


@router.message(RFState.set_terms)
async def msg_terms(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    value = None if raw == "0" else (message.html_text or message.text).strip()
    async with db.session() as session:
        await settings_service.set(session, settings_service.REF_TERMS, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К рефералам", "rf_home")).as_markup()
    await message.answer("✅ Условия обновлены", reply_markup=kb)

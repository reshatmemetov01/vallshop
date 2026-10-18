"""Пользовательские хендлеры бонусов: ввод промокода (Профиль) и клик по кнопке
«забрать бонус» в посте бонус-рассылки. Работают во всех ботах (merchant_id из
middleware определяет магазин)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import balance as balance_service
from ..services import bonuses as bonuses_service
from ..services import orders as order_service
from .. import keyboards as kb
from .. import texts

router = Router(name="bonus_user")


class PromoFlow(StatesGroup):
    enter_code = State()


def _back_profile():
    return InlineKeyboardBuilder().row(kb._b("⬅️ В профиль", callback_data="profile")).as_markup()


# ── Промокод (Профиль → Промокод) ────────────────────────────────────────────
@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoFlow.enter_code)
    b = InlineKeyboardBuilder().row(kb._b("⬅️ Отмена", callback_data="profile"))
    await call.message.answer(
        "🎟 <b>Промокод</b>\n\nВведите промокод — бонус зачислится на баланс.",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.message(PromoFlow.enter_code)
async def msg_promo_code(
    message: Message, db: Database, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    async with db.session() as s:
        await order_service.ensure_user(
            s, message.from_user.id, message.from_user.username,
            message.from_user.full_name, merchant_id,
        )
        await s.commit()
    status, amount = await bonuses_service.redeem(
        db, merchant_id, message.from_user.id, message.text or ""
    )
    if status == "ok":
        async with db.session() as s:
            bal = await balance_service.get_balance(s, message.from_user.id, merchant_id)
        await message.answer(
            f"✅ Промокод активирован! Начислено <b>{texts.rub(amount)}</b> на баланс.\n"
            f"Баланс: <b>{texts.rub(bal)}</b>",
            reply_markup=_back_profile(),
        )
        return
    reasons = {
        "not_found": "❌ Промокод не найден или больше не действует.",
        "already": "Вы уже активировали этот промокод.",
        "exhausted": "❌ Лимит активаций этого промокода исчерпан.",
        "nofunds": "❌ Промокод временно недоступен. Попробуйте позже.",
    }
    await message.answer(reasons.get(status, "❌ Не удалось активировать промокод."),
                         reply_markup=_back_profile())


# ── Клик по кнопке «забрать бонус» в посте бонус-рассылки ─────────────────────
def _success_kb(info: dict):
    label = info.get("success_btn_label")
    target = info.get("success_btn_target")
    kind = info.get("success_btn_kind")
    if not label or not target:
        return None
    return kb.broadcast_post_kb(
        [{"label": label, "kind": kind or "cb", "target": target,
          "style": "success", "icon": None}]
    )


@router.callback_query(F.data.startswith("bclaim:"))
async def cb_bonus_claim(
    call: CallbackQuery, db: Database, merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    try:
        drop_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer()
        return
    async with db.session() as s:
        await order_service.ensure_user(
            s, call.from_user.id, call.from_user.username,
            call.from_user.full_name, merchant_id,
        )
        await s.commit()

    status, info = await bonuses_service.claim(db, merchant_id, drop_id, call.from_user.id)

    if status == "already":
        await call.answer("Вы уже получили этот бонус 🙂", show_alert=True)
        return
    if status == "not_found":
        await call.answer("Акция недоступна", show_alert=True)
        return
    if status == "ended":
        err = info.get("error_text") or texts.BONUS_ENDED_DEFAULT
        sent = await call.message.answer(err)
        await bonuses_service.store_message(db, drop_id, sent.chat.id, sent.message_id)
        await call.answer()
        return
    # ok
    from decimal import Decimal
    amount = info.get("amount") or Decimal(0)
    body = info.get("success_text") or texts.BONUS_SUCCESS_DEFAULT.format(amount=texts.rub(amount))
    sent = await call.message.answer(body, reply_markup=_success_kb(info))
    await bonuses_service.store_message(db, drop_id, sent.chat.id, sent.message_id)
    await call.answer("Бонус зачислен ✅")

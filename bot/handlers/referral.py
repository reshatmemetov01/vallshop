"""Реферальная программа (клиентская часть): экран «Заработок» и «Реферальная система»."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import referral as referral_service
from ..services import settings as settings_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router(name="referral")


@router.callback_query(F.data == "earn")
async def cb_earn(
    call: CallbackQuery, state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    await state.clear()
    # Франшизу можно создать только из головного магазина.
    show_franchise = merchant_id == HEAD_MERCHANT_ID
    await render(
        call, banner="main", caption=texts.EARN_TITLE,
        reply_markup=kb.earn_kb(show_franchise=show_franchise),
    )
    await call.answer()


async def _referral_screen(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        enabled = await settings_service.get_bool(
            session, settings_service.REF_ENABLED, True
        )
        if not enabled:
            await render(call, banner="main", caption=texts.REFERRAL_DISABLED,
                         reply_markup=kb.earn_kb())
            return
        st = await referral_service.stats(session, call.from_user.id, merchant_id)
        terms = await referral_service.terms_text(session)
        min_raw = await settings_service.get(session, settings_service.REF_MIN_WD_RUB)
        min_rub = Decimal(min_raw) if min_raw else referral_service.DEFAULT_MIN_RUB

    me = await call.message.bot.get_me()
    link = referral_service.ref_link(me.username, call.from_user.id)

    lines = [
        "👥 <b>Реферальная система</b>",
        "",
        terms,
        "",
        "🔗 Ваша ссылка:",
        f"<code>{link}</code>",
        "",
        f"Приглашено: <b>{st['invited']}</b>",
        f"Из них с покупками: <b>{st['with_purchase']}</b>",
        "",
        f"💰 Реферальный баланс: <b>{texts.rub(st['bal_rub'])}</b>",
        f"Заработано всего: <b>{texts.rub(st['earned_rub'])}</b>",
    ]

    can_transfer = st["bal_rub"] >= min_rub and st["bal_rub"] > 0
    if not can_transfer:
        lines.append("")
        lines.append(f"ℹ️ Перевод на основной баланс — от {texts.rub(min_rub)}.")

    await render(
        call, banner="main", caption="\n".join(lines),
        reply_markup=kb.referral_kb(can_transfer),
    )


@router.callback_query(F.data == "ref")
async def cb_ref(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    await _referral_screen(call, db, config, merchant_id)
    await call.answer()


@router.callback_query(F.data == "ref_transfer")
async def cb_ref_transfer(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        ok, amount, need = await referral_service.transfer_to_balance(
            session, call.from_user.id, "RUB", merchant_id
        )
        await session.commit()
    if not ok:
        await call.answer(
            f"Недостаточно для перевода. Минимум {texts.rub(need)}.", show_alert=True
        )
        return
    await call.answer(f"Переведено {texts.rub(amount)} на основной баланс ✅", show_alert=True)
    await _referral_screen(call, db, config, merchant_id)

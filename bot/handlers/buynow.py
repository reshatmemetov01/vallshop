"""«Купить сразу»: при нехватке баланса — доплата под конкретный товар.

Вместо «пополните баланс» бот предлагает оплатить НЕДОСТАЮЩУЮ сумму тем же
набором способов (карта/СБП/USDT). Счёт создаётся как обычное пополнение, но
с «намерением покупки» (``TopUp.buy_json``). После оплаты баланс зачисляется и
товар докупается/выдаётся автоматически (см. ``services/buynow.py``). Баланс до
оплаты не трогается; «Отмена» ничего не списывает.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, TopUp, TopUpStatus
from ..payments import PaymentProvider
from ..services import balance as balance_service
from ..services import buynow as buynow_service
from ..services import orders as order_service
from ..services import paymethods as paymethods_service
from ..services import rates as rates_service
from ..services.balance import TOPUP_MIN
from ..payments.boltutil import MIN_PAYMENT_USDT
from .. import keyboards as kb
from .. import texts

router = Router(name="buynow")


class BuyNowFlow(StatesGroup):
    choosing = State()   # выбор способа оплаты
    network = State()    # выбор сети USDT


def _availability(config: BotConfig) -> dict:
    return {
        "payhot": config.payhot_enabled,
        "paydex": config.paydex_enabled,
        "platega": config.platega_enabled,
        "tbank": config.tbank_enabled,
        "bolt": True,
    }


def _rub_provider(method_id, payhot, paydex, platega, tbank):
    name = paymethods_service.PROVIDER_OF.get(method_id)
    return {"payhot": payhot, "paydex": paydex, "platega": platega, "tbank": tbank}.get(name)


def _min_usdt_rub() -> Decimal:
    rate = rates_service.get_rate()
    dyn = (MIN_PAYMENT_USDT * rate).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return max(TOPUP_MIN, dyn)


async def offer(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext, *,
    variant_id: int, unit_price, owner_price, qty: int,
    merchant_id: int, user_id: int, title: str | None = None,
    fields: dict | None = None, smm_link: str | None = None,
    smm_quantity: int | None = None,
) -> None:
    """Предложить доплату под товар (вызывается из веток «не хватает баланса»)."""
    from ..services import catalog as catalog_service
    total = Decimal(str(unit_price)) * qty
    async with db.session() as session:
        balance = await balance_service.get_balance(session, user_id, merchant_id=merchant_id)
        methods = await paymethods_service.client_methods(session, available=_availability(config))
        if not title:
            v = await catalog_service.get_variant(session, int(variant_id))
            title = getattr(v, "title", "товар") if v else "товар"
    short = total - balance
    if short < 0:
        short = Decimal("0")
    if not methods:
        await call.message.answer(
            "Оплата временно недоступна — напишите в поддержку.",
            reply_markup=kb.not_enough_balance_kb(),
        )
        await call.answer()
        return

    intent = buynow_service.make_intent(
        variant_id=variant_id, qty=qty, unit_price=unit_price, owner_price=owner_price,
        title=title, fields=fields, smm_link=smm_link, smm_quantity=smm_quantity,
    )
    await state.set_state(BuyNowFlow.choosing)
    await state.update_data(
        bn_intent=intent, bn_short=str(short), bn_mid=merchant_id, bn_title=title,
    )
    charge = max(short, TOPUP_MIN)
    await call.message.answer(
        texts.BUY_NOW_OFFER.format(
            title=title, price=texts.rub(total), balance=texts.rub(balance),
            short=texts.rub(short), charge=texts.rub(charge),
        ),
        reply_markup=kb.buynow_methods_kb(methods),
    )
    await call.answer()


@router.callback_query(F.data == "bncancel")
async def bn_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await call.message.answer(texts.BUY_NOW_CANCELLED)
    await call.answer("Отменено")


@router.callback_query(F.data.startswith("bncxl:"))
async def bn_cancel_invoice(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await call.message.answer(texts.BUY_NOW_CANCELLED)
    await call.answer("Отменено")


@router.callback_query(BuyNowFlow.choosing, F.data.startswith("bnm:"))
async def bn_method(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    provider: PaymentProvider, payhot: PaymentProvider = None,
    paydex: PaymentProvider = None, platega: PaymentProvider = None,
    tbank: PaymentProvider = None, merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    method = call.data.split(":", 1)[1].lower()
    data = await state.get_data()
    if "bn_intent" not in data:
        await call.answer("Сессия устарела, начните заново", show_alert=True)
        return

    if method == "usdt":
        nets = config.networks
        if len(nets) <= 1:
            await _create_and_send(call, db, config, state, provider, "usdt", nets[0] if nets else config.bolt_network, merchant_id)
        else:
            await state.set_state(BuyNowFlow.network)
            try:
                await call.message.edit_reply_markup(reply_markup=kb.buynow_networks_kb(nets))
            except Exception:  # noqa: BLE001
                await call.message.answer("Выберите сеть USDT:", reply_markup=kb.buynow_networks_kb(nets))
            await call.answer()
        return

    pay = _rub_provider(method, payhot, paydex, platega, tbank)
    if pay is None:
        await call.answer("Способ недоступен", show_alert=True)
        return
    await _create_and_send(call, db, config, state, pay, method, None, merchant_id)


@router.callback_query(BuyNowFlow.network, F.data.startswith("bnnet:"))
async def bn_network(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    provider: PaymentProvider, merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    net = call.data.split(":", 1)[1].upper()
    if net not in config.networks:
        await call.answer("Сеть недоступна", show_alert=True)
        return
    await _create_and_send(call, db, config, state, provider, "usdt", net, merchant_id)


async def _create_and_send(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    pay, method: str, net: str | None, merchant_id: int,
) -> None:
    data = await state.get_data()
    intent = data.get("bn_intent")
    if intent is None:
        await call.answer("Сессия устарела", show_alert=True)
        return
    short = Decimal(data.get("bn_short", "0"))

    if method == "usdt":
        charge = max(short, _min_usdt_rub())
    else:
        charge = max(short, TOPUP_MIN)

    async with db.session() as session:
        await order_service.ensure_user(
            session, call.from_user.id, call.from_user.username,
            call.from_user.full_name, merchant_id,
        )
        method_label = await paymethods_service.label(session, method)
        topup = await balance_service.create_topup(
            session, user_id=call.from_user.id, amount_usd=charge, merchant_id=merchant_id,
        )
        topup.buy_json = intent
        try:
            if method == "usdt":
                rate = rates_service.get_rate()
                usdt = (charge / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                invoice = await pay.create_invoice(
                    amount=usdt, client_ref=topup.client_ref,
                    description=f"Buy {call.from_user.id}",
                    notify_url=config.notify_url, success_url=config.public_base_url,
                    network=net,
                )
                topup.currency = invoice.currency
                topup.address = invoice.address
                topup.pay_amount = invoice.amount
            else:
                notify_url = {
                    "paydex": config.paydex_notify_url,
                    "platega": config.platega_notify_url,
                    "tbank": config.tbank_notify_url,
                }.get(pay.name, config.payhot_notify_url)
                invoice = await pay.create_invoice(
                    amount=charge, client_ref=topup.client_ref,
                    description=f"Buy {call.from_user.id}",
                    notify_url=notify_url, success_url=config.public_base_url,
                    network=paymethods_service.method_code(method),
                )
                topup.currency = "RUB"
                topup.pay_amount = charge
        except Exception:  # noqa: BLE001
            await session.rollback()
            await call.message.answer(
                "Не удалось создать счёт. Попробуйте другой способ или позже.",
                reply_markup=kb.buynow_methods_kb(
                    await paymethods_service.client_methods(session, available=_availability(config))
                ),
            )
            await call.answer()
            return
        topup.provider = pay.name
        topup.provider_order_id = invoice.provider_order_id
        topup.checkout_url = invoice.checkout_url
        topup.network = invoice.network
        topup.expires_at = invoice.expires_at
        await session.commit()
        topup_id = topup.id

    await state.clear()
    if method == "usdt":
        await call.message.answer(
            texts.TOPUP_CREATED.format(
                credit=texts.rub(charge),
                amount=texts.money(invoice.amount, invoice.currency),
                network=invoice.network, address=invoice.address,
            ),
            reply_markup=kb.buynow_payment_kb(topup_id, invoice.checkout_url, invoice.address),
        )
    else:
        await call.message.answer(
            texts.BUY_NOW_LINK.format(charge=texts.rub(charge), method=method_label),
            reply_markup=kb.buynow_payment_kb(topup_id, invoice.checkout_url),
        )
    await call.answer()

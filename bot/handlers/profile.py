"""Профиль покупателя: баланс, пополнение (крипта), заказы, история пополнений.

Все экраны — фото-баннер «Профиль» с подписью и inline-кнопками (см. bot.ui).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape as _esc

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Order, OrderStatus, TopUp, TopUpStatus, Variant
from ..payments import PaymentProvider
from ..services import balance as balance_service
from ..services import notify as notify_service
from ..services import orders as order_service
from ..services import paymethods as paymethods_service
from ..services import rates as rates_service
from ..services import smm as smm_service
from ..services.balance import TOPUP_MAX, TOPUP_MIN
from ..ui import render
from .. import keyboards as kb
from .. import texts
from socproof import SocProofClient

router = Router()

# Минимум платежа крипто-провайдера (BoltUtil) — 1 USDT. Рублёвый минимум
# пополнения не может быть ниже его эквивалента по текущему курсу.
PROVIDER_MIN_USDT = Decimal("1")


def _min_topup_rub() -> Decimal:
    rate = rates_service.get_rate()
    dyn = (PROVIDER_MIN_USDT * rate).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return max(TOPUP_MIN, dyn)


class TopUpFlow(StatesGroup):
    waiting_amount = State()
    waiting_network = State()


def _status_ru(status) -> str:
    key = str(getattr(status, "value", status))  # строка или OrderStatus-enum
    return {
        "completed": "✅ выполнен",
        "created": "🕒 создаётся",
        "awaiting_payment": "⏳ ожидает оплату",
        "paid": "💳 оплачен",
        "fulfilling": "⏳ выдаётся",
        "failed": "⚠️ ошибка",
        "needs_action": "⚠️ в обработке",
        "expired": "❌ просрочен",
        "refunded": "↩️ возврат",
        "pending": "⏳ ожидает",
    }.get(key, key)


async def _profile_caption(
    session, config: BotConfig, user_id: int, merchant_id: int = HEAD_MERCHANT_ID
) -> str:
    balance = await balance_service.get_balance(session, user_id, merchant_id)
    orders = await session.scalar(
        select(func.count()).select_from(Order).where(
            Order.user_id == user_id, Order.merchant_id == merchant_id
        )
    )
    from ..services import menu as menu_service
    return menu_service.caption(
        "profile", texts.PROFILE, merchant_id,
        user_id=user_id, balance=texts.rub(balance), orders=int(orders or 0),
    )


async def _show_profile(
    event, db: Database, config: BotConfig, user_id: int,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        caption = await _profile_caption(session, config, user_id, merchant_id)
    await render(event, banner="profile", caption=caption, reply_markup=kb.profile_kb(merchant_id))


@router.message(F.text == "🟢 Мой профиль")
async def msg_profile(
    message: Message, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        await order_service.ensure_user(
            session, message.from_user.id, message.from_user.username,
            message.from_user.full_name, merchant_id,
        )
        await session.commit()
    await _show_profile(message, db, config, message.from_user.id, merchant_id)


@router.callback_query(F.data == "profile")
async def cb_profile(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    await _show_profile(call, db, config, call.from_user.id, merchant_id)
    await call.answer()


@router.callback_query(F.data == "balance")
async def cb_balance(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    async with db.session() as session:
        balance = await balance_service.get_balance(session, call.from_user.id, merchant_id)
    await render(
        call,
        banner="profile",
        caption=texts.BALANCE_VIEW.format(balance=texts.rub(balance)),
        reply_markup=kb.balance_kb(),
    )
    await call.answer()


def _provider_availability(config: BotConfig) -> dict:
    """Карта provider_name -> доступен ли (настроен) провайдер."""
    return {
        "payhot": config.payhot_enabled,
        "paydex": config.paydex_enabled,
        "bolt": True,  # крипта всегда доступна (реальный провайдер или mock)
    }


def _rub_provider(method_id: str, payhot, paydex):
    """Платёжный провайдер для рублёвого способа (card → PayHot, sbp → Paydex)."""
    prov_name = paymethods_service.PROVIDER_OF.get(method_id)
    if prov_name == "payhot":
        return payhot
    if prov_name == "paydex":
        return paydex
    return None


# ── Пополнение (сначала способ, затем сумма) ─────────────────────────────────
@router.callback_query(F.data == "topup")
async def cb_topup(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext
) -> None:
    await state.clear()
    available = _provider_availability(config)
    async with db.session() as session:
        methods = await paymethods_service.client_methods(session, available=available)
        heading = await paymethods_service.heading(session)
    await render(
        call,
        banner="profile",
        caption=heading,
        reply_markup=kb.topup_methods_kb(methods),
    )
    await call.answer()


# ── Пополнение ₽ (карта → PayHot / СБП → Paydex) ─────────────────────────────
@router.callback_query(F.data.startswith("tu_rub:"))
async def cb_topup_rub(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext,
    payhot: PaymentProvider = None, paydex: PaymentProvider = None,
) -> None:
    method = call.data.split(":", 1)[1].lower()
    available = _provider_availability(config)
    async with db.session() as session:
        methods = {m["id"]: m for m in await paymethods_service.client_methods(session, available=available)}
    if method not in methods or _rub_provider(method, payhot, paydex) is None:
        await call.answer("Способ недоступен", show_alert=True)
        return
    await state.set_state(TopUpFlow.waiting_amount)
    await state.update_data(kind="rub", method=method)
    await render(
        call,
        banner="profile",
        caption=texts.TOPUP_ASK_AMOUNT.format(min=int(TOPUP_MIN), max=TOPUP_MAX),
        reply_markup=kb.topup_cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "tu_usdt")
async def cb_topup_usdt(call: CallbackQuery, config: BotConfig, state: FSMContext) -> None:
    await state.set_state(TopUpFlow.waiting_network)
    await render(
        call,
        banner="profile",
        caption=texts.TOPUP_CHOOSE_METHOD,
        reply_markup=kb.topup_networks_kb(config.networks),
    )
    await call.answer()


@router.callback_query(TopUpFlow.waiting_network, F.data.startswith("tunet:"))
async def cb_topup_network(
    call: CallbackQuery, config: BotConfig, state: FSMContext
) -> None:
    net = call.data.split(":", 1)[1].upper()
    if net not in config.networks:
        await call.answer("Сеть недоступна", show_alert=True)
        return
    await state.update_data(net=net)
    await state.set_state(TopUpFlow.waiting_amount)
    await render(
        call,
        banner="profile",
        caption=texts.TOPUP_ASK_AMOUNT.format(min=_min_topup_rub(), max=TOPUP_MAX),
        reply_markup=kb.topup_cancel_kb(),
    )
    await call.answer()


@router.message(TopUpFlow.waiting_amount)
async def msg_topup_amount(
    message: Message,
    db: Database,
    config: BotConfig,
    provider: PaymentProvider,
    state: FSMContext,
    payhot: PaymentProvider = None,
    paydex: PaymentProvider = None,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    data = await state.get_data()
    kind = data.get("kind", "usdt")
    raw = (message.text or "").strip().replace(",", ".")
    min_rub = int(TOPUP_MIN) if kind == "rub" else _min_topup_rub()
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        await message.answer(texts.TOPUP_BAD_AMOUNT.format(min=min_rub, max=TOPUP_MAX))
        return
    if amount < Decimal(min_rub) or amount > TOPUP_MAX:
        await message.answer(texts.TOPUP_BAD_AMOUNT.format(min=min_rub, max=TOPUP_MAX))
        return

    # ── ₽ (карта → PayHot / СБП → Paydex) ────────────────────────────────────
    if kind == "rub":
        method = (data.get("method") or "").lower()
        pay = _rub_provider(method, payhot, paydex)
        if pay is None:
            await state.clear()
            await message.answer("Способ оплаты недоступен, начните пополнение заново.")
            return
        await state.clear()
        async with db.session() as session:
            await order_service.ensure_user(
                session, message.from_user.id, message.from_user.username,
                message.from_user.full_name, merchant_id,
            )
            method_label = await paymethods_service.label(session, method)
            topup = await balance_service.create_topup(
                session, user_id=message.from_user.id, amount_usd=amount,
                merchant_id=merchant_id,
            )
            notify_url = (
                config.paydex_notify_url if pay.name == "paydex"
                else config.payhot_notify_url
            )
            try:
                invoice = await pay.create_invoice(
                    amount=amount, client_ref=topup.client_ref,
                    description=f"Balance top-up {message.from_user.id}",
                    notify_url=notify_url,
                    success_url=config.public_base_url,
                    network=paymethods_service.method_code(method),
                )
            except Exception:  # noqa: BLE001
                await session.rollback()
                await message.answer("Не удалось создать счёт. Попробуйте другой способ или позже.")
                raise
            topup.provider = pay.name
            topup.provider_order_id = invoice.provider_order_id
            topup.checkout_url = invoice.checkout_url
            topup.network = invoice.network
            topup.pay_amount = amount
            topup.currency = "RUB"
            topup.expires_at = invoice.expires_at
            await session.commit()
            topup_id = topup.id
        await render(
            message, banner="profile",
            caption=texts.TOPUP_CREATED_LINK.format(
                credit=texts.rub(amount), method=method_label
            ),
            reply_markup=kb.topup_payment_kb(topup_id, invoice.checkout_url),
        )
        return

    # ── USDT (BoltUtil) — существующий флоу ──────────────────────────────────
    net = (data.get("net") or "").upper()
    if net not in config.networks:
        await state.clear()
        await message.answer("Сеть не выбрана, начните пополнение заново.")
        return
    await state.clear()

    # Баланс рублёвый: клиент вводит сумму в ₽, платит криптой в USDT по курсу.
    rate = rates_service.get_rate()
    usdt_to_pay = (amount / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async with db.session() as session:
        await order_service.ensure_user(
            session, message.from_user.id, message.from_user.username,
            message.from_user.full_name, merchant_id,
        )
        # amount_usd хранит сумму ЗАЧИСЛЕНИЯ — теперь это рубли.
        topup = await balance_service.create_topup(
            session, user_id=message.from_user.id, amount_usd=amount,
            merchant_id=merchant_id,
        )
        try:
            invoice = await provider.create_invoice(
                amount=usdt_to_pay,
                client_ref=topup.client_ref,
                description=f"Balance top-up {message.from_user.id}",
                notify_url=config.notify_url,
                success_url=config.public_base_url,
                network=net,
            )
        except Exception:  # noqa: BLE001
            await session.rollback()
            await message.answer(
                "Не удалось создать счёт. Попробуйте другую сеть или позже."
            )
            raise

        topup.provider = provider.name
        topup.provider_order_id = invoice.provider_order_id
        topup.checkout_url = invoice.checkout_url
        topup.address = invoice.address
        topup.network = invoice.network
        topup.pay_amount = invoice.amount
        topup.currency = invoice.currency
        topup.expires_at = invoice.expires_at
        await session.commit()
        topup_id = topup.id

    await render(
        message,
        banner="profile",
        caption=texts.TOPUP_CREATED.format(
            credit=texts.rub(amount),
            amount=texts.money(invoice.amount, invoice.currency),
            network=invoice.network,
            address=invoice.address,
        ),
        reply_markup=kb.topup_payment_kb(topup_id, invoice.checkout_url, invoice.address),
    )


@router.callback_query(F.data.startswith("tucheck:"))
async def cb_topup_check(
    call: CallbackQuery, db: Database, config: BotConfig, provider: PaymentProvider,
    payhot: PaymentProvider = None, paydex: PaymentProvider = None,
    bots_by_merchant: dict = None,
) -> None:
    topup_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        topup = await session.get(TopUp, topup_id)
        if not topup or topup.user_id != call.from_user.id:
            await call.answer("Пополнение не найдено", show_alert=True)
            return

        mid = topup.merchant_id
        if topup.status == TopUpStatus.PAID:
            balance = await balance_service.get_balance(session, call.from_user.id, mid)
            await render(
                call, banner="profile",
                caption=texts.TOPUP_SUCCESS.format(
                    credit=texts.rub(topup.amount_usd),
                    balance=texts.rub(balance),
                ),
                reply_markup=kb.back_profile_kb(),
            )
            await call.answer()
            return

        if (
            topup.expires_at
            and datetime.utcnow() > topup.expires_at
            and topup.status == TopUpStatus.PENDING
        ):
            topup.status = TopUpStatus.EXPIRED
            await session.commit()
            await render(
                call, banner="profile", caption=texts.TOPUP_EXPIRED,
                reply_markup=kb.back_profile_kb(),
            )
            await call.answer()
            return

        if topup.provider == "payhot" and payhot is not None:
            prov, ref = payhot, topup.provider_order_id
        elif topup.provider == "paydex" and paydex is not None:
            prov, ref = paydex, topup.provider_order_id
        else:
            prov, ref = provider, topup.client_ref
        if not ref:
            await call.answer(texts.TOPUP_PENDING, show_alert=True)
            return
        update = await prov.get_status(ref)
        if update.status != "paid":
            await call.answer(texts.TOPUP_PENDING, show_alert=True)
            return

        topup.status = TopUpStatus.PAID
        topup.tx_hash = update.tx_hash
        topup.paid_at = datetime.utcnow()
        await balance_service.credit(session, call.from_user.id, topup.amount_usd, merchant_id=mid)
        await session.commit()
        credited = topup.amount_usd
        balance = await balance_service.get_balance(session, call.from_user.id, mid)

    await render(
        call,
        banner="profile",
        caption=texts.TOPUP_SUCCESS.format(
            credit=texts.rub(credited),
            balance=texts.rub(balance),
        ),
        reply_markup=kb.back_profile_kb(),
    )
    await call.answer("Баланс пополнен ✅")
    # Уведомить админов (головного — всегда, владельца франшизы — если франшиза).
    await notify_service.notify_topup(
        db, config, bots_by_merchant, user_id=call.from_user.id, amount=credited, merchant_id=mid
    )


# ── Списки ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "myorders")
async def cb_my_orders(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        rows = await session.execute(
            select(Order, Variant.title)
            .join(Variant, Variant.id == Order.variant_id)
            .where(Order.user_id == call.from_user.id, Order.merchant_id == merchant_id)
            .order_by(Order.id.desc())
            .limit(20)
        )
        items = rows.all()

    b = InlineKeyboardBuilder()
    if not items:
        caption = texts.MY_ORDERS_TITLE + "\n\n" + texts.NO_ORDERS
    else:
        caption = texts.MY_ORDERS_TITLE + "\n\nВыберите заказ, чтобы посмотреть детали:"
        for order, vtitle in items:
            try:
                label = _order_button_label(order, vtitle)
            except Exception:  # noqa: BLE001 — «кривой» заказ не ломает список
                label = f"{vtitle}"
            b.row(InlineKeyboardButton(text=label, callback_data=f"order:{order.id}"))
    b.row(InlineKeyboardButton(text="⬅️ В профиль", callback_data="profile"))
    await render(call, banner="profile", caption=caption, reply_markup=b.as_markup())
    await call.answer()


def _order_fields(order) -> dict:
    """Безопасно распарсить fields_json SMM-заказа в словарь."""
    try:
        parsed = json.loads(order.fields_json) if order.fields_json else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _order_status_emoji(order) -> str:
    s = order.status
    if s == OrderStatus.COMPLETED:
        return "✅"
    if s == OrderStatus.FULFILLING:
        return "🚀" if order.supplier == "socproof" else "⏳"
    if s == OrderStatus.REFUNDED:
        return "↩️"
    if s in (OrderStatus.FAILED, OrderStatus.NEEDS_ACTION):
        return "⚠️"
    return "🕒"


def _order_button_label(order, vtitle: str) -> str:
    """Короткая подпись кнопки заказа в списке (без нумерации)."""
    total = Decimal(order.price_usd or 0) * (order.quantity or 1)
    name = vtitle if len(vtitle) <= 26 else vtitle[:25] + "…"
    extra = ""
    if order.supplier == "socproof" and order.status == OrderStatus.FULFILLING:
        prog = _order_fields(order).get("progress")
        if isinstance(prog, int):
            extra = f" · {prog}%"
    return f"{_order_status_emoji(order)} {name} · {texts.rub(total)}{extra}"


def _order_detail_caption(order, vtitle: str) -> str:
    """Подробности заказа для экрана деталей."""
    total = Decimal(order.price_usd or 0) * (order.quantity or 1)
    name = _esc(vtitle)
    lines = [f"🧾 <b>{name}</b>", f"Заказ: <code>#{order.id}</code>"]
    if order.supplier == "socproof":
        f = _order_fields(order)
        qty = f.get("quantity") or order.quantity or "?"
        lines.append(f"Количество: <b>{qty}</b>")
        lines.append(f"Сумма: <b>{texts.rub(total)}</b>")
        link = f.get("link")
        if link:
            lines.append(f"Ссылка: {_esc(str(link))}")
        if order.status == OrderStatus.FULFILLING:
            st = smm_service.status_label(f.get("status"))
            prog = f.get("progress")
            lines.append(f"Статус: {st}" + (f" · {prog}%" if isinstance(prog, int) else ""))
            rem = f.get("remains")
            if isinstance(rem, int) and isinstance(qty, int):
                lines.append(f"Выполнено: <b>{qty - rem}</b> из <b>{qty}</b>")
        elif order.status == OrderStatus.REFUNDED:
            lines.append("Статус: ↩️ <b>возврат</b> (деньги возвращены на баланс)")
        elif (order.status == OrderStatus.COMPLETED and order.fail_reason
              and "частично" in order.fail_reason):
            lines.append("Статус: ⚠️ <b>частично выполнен</b> (часть возвращена на баланс)")
        else:
            lines.append(f"Статус: {_order_status_emoji(order)} {_status_ru(order.status)}")
    else:
        lines.append(f"Количество: <b>{order.quantity or 1}</b>")
        lines.append(f"Сумма: <b>{texts.rub(total)}</b>")
        lines.append(f"Статус: {_order_status_emoji(order)} {_status_ru(order.status)}")
        if order.status == OrderStatus.COMPLETED and order.delivery_code:
            codes = _esc("\n".join(order.delivery_code.splitlines()))
            lines.append(f"\nВыдача (нажмите, чтобы скопировать):\n<code>{codes}</code>")
    if order.created_at:
        dt = order.created_at + timedelta(hours=3)  # МСК
        lines.append(f"\n🗓 {dt.strftime('%d.%m.%Y %H:%M')} (МСК)")
    return "\n".join(lines)


async def _show_order_detail(call, db, order_id, merchant_id) -> bool:
    """Отрисовать детали заказа. Возвращает False, если заказ не найден."""
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != call.from_user.id or order.merchant_id != merchant_id:
            return False
        variant = await session.get(Variant, order.variant_id)
        vtitle = variant.title if variant else "Товар"
        caption = _order_detail_caption(order, vtitle)
        is_smm_active = order.supplier == "socproof" and order.status == OrderStatus.FULFILLING
    b = InlineKeyboardBuilder()
    if is_smm_active:
        b.row(InlineKeyboardButton(text="🔄 Обновить статус", callback_data=f"ordref:{order_id}"))
    b.row(InlineKeyboardButton(text="⬅️ К заказам", callback_data="myorders"))
    await render(call, banner="profile", caption=caption, reply_markup=b.as_markup())
    return True


@router.callback_query(F.data.startswith("order:"))
async def cb_order_detail(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    order_id = int(call.data.split(":", 1)[1])
    if not await _show_order_detail(call, db, order_id, merchant_id):
        await call.answer("Заказ не найден", show_alert=True)
        return
    await call.answer()


@router.callback_query(F.data.startswith("ordref:"))
async def cb_order_refresh(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID, soc: SocProofClient = None,
) -> None:
    """Обновить статус одного SMM-заказа по запросу клиента."""
    order_id = int(call.data.split(":", 1)[1])
    if soc is None or not getattr(soc, "configured", False):
        await call.answer("Обновление сейчас недоступно", show_alert=True)
        return
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if (order and order.user_id == call.from_user.id
                and order.merchant_id == merchant_id
                and order.supplier == "socproof"
                and order.status == OrderStatus.FULFILLING):
            try:
                await order_service.settle_socproof(session, order, soc)
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()
    await _show_order_detail(call, db, order_id, merchant_id)
    await call.answer("Обновлено ✅")


@router.callback_query(F.data == "mytopups")
async def cb_my_topups(
    call: CallbackQuery, db: Database, config: BotConfig,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        topups = await balance_service.list_topups(session, call.from_user.id, merchant_id)

    if not topups:
        caption = texts.MY_TOPUPS_TITLE + "\n\n" + texts.NO_TOPUPS
    else:
        lines = [texts.MY_TOPUPS_TITLE, ""]
        for t in topups:
            lines.append(
                f"#{t.id} · {texts.rub(t.amount_usd)} · "
                f"{(t.network or '-')} · {_status_ru(t.status)}"
            )
        caption = "\n".join(lines)

    await render(call, banner="profile", caption=caption, reply_markup=kb.back_profile_kb())
    await call.answer()

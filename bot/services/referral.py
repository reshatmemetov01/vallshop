"""Реферальная программа: привязка, начисления (модель A — % с покупок), сторно, перевод.

Этап 1: активна валюта USDT (валюта текущих заказов). Поля/логика для RUB
заложены — «оживут» вместе с рублёвой оплатой (у заказа появится валюта).
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, Order, OrderStatus, ReferralEarning, User
from .users import get_user
from . import balance as balance_service
from . import settings as settings_service

log = logging.getLogger("vallshop.referral")

DEFAULT_PERCENT = Decimal("5")
DEFAULT_MIN_USDT = Decimal("1")
DEFAULT_MIN_RUB = Decimal("50")
DEFAULT_TERMS = (
    "Приглашай друзей по своей ссылке и получай <b>5%</b> с каждой их покупки "
    "на реферальный баланс. Накопленное можно перевести на основной баланс и "
    "тратить на товары."
)

_Q = Decimal("0.01")


def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def parse_ref_payload(payload: Optional[str]) -> Optional[int]:
    """Из '/start ref_123' достаём 123."""
    if not payload:
        return None
    payload = payload.strip()
    if payload.startswith("ref_"):
        try:
            return int(payload[4:])
        except ValueError:
            return None
    return None


async def bind_referral(session: AsyncSession, user: User, referrer_id: int) -> bool:
    """Привязать пригласившего к НОВОМУ пользователю (однократно, не на себя)."""
    if user.referred_by is not None:
        return False
    if referrer_id == user.id:
        return False
    referrer = await get_user(session, referrer_id, user.merchant_id)
    if referrer is None:
        return False
    user.referred_by = referrer_id
    await session.flush()
    return True


async def _percent(session: AsyncSession) -> Decimal:
    raw = await settings_service.get(session, settings_service.REF_PERCENT)
    try:
        return Decimal(raw) if raw else DEFAULT_PERCENT
    except Exception:  # noqa: BLE001
        return DEFAULT_PERCENT


async def accrue_for_order(session: AsyncSession, order: Order) -> None:
    """Начислить реферальный бонус пригласившему покупателя (идемпотентно)."""
    if order.status != OrderStatus.COMPLETED:
        return
    if not await settings_service.get_bool(session, settings_service.REF_ENABLED, True):
        return
    buyer = await get_user(session, order.user_id, order.merchant_id)
    if not buyer or not buyer.referred_by:
        return
    # Идемпотентность: по одному заказу — одно начисление.
    exists = await session.scalar(
        select(ReferralEarning.id).where(
            ReferralEarning.order_id == order.id,
            ReferralEarning.kind == "purchase_percent",
        )
    )
    if exists:
        return
    referrer = await get_user(session, buyer.referred_by, order.merchant_id)
    if referrer is None:
        return
    percent = await _percent(session)
    # order.price_usd хранит сумму списания в рублях (валюта магазина — ₽).
    total = Decimal(order.price_usd) * (order.quantity or 1)
    amount = (total * percent / Decimal(100)).quantize(_Q, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return
    currency = "RUB"
    referrer.ref_balance_rub = Decimal(referrer.ref_balance_rub or 0) + amount
    referrer.ref_earned_rub = Decimal(referrer.ref_earned_rub or 0) + amount
    session.add(ReferralEarning(
        merchant_id=order.merchant_id,
        referrer_id=referrer.id, referral_id=buyer.id, order_id=order.id,
        kind="purchase_percent", currency=currency, amount=amount, status="credited",
    ))
    await session.flush()


async def reverse_for_order(session: AsyncSession, order: Order) -> None:
    """Сторнировать начисление при возврате/отмене заказа (идемпотентно)."""
    earning = await session.scalar(
        select(ReferralEarning).where(
            ReferralEarning.order_id == order.id,
            ReferralEarning.status == "credited",
        )
    )
    if earning is None:
        return
    referrer = await get_user(session, earning.referrer_id, earning.merchant_id)
    if referrer is not None:
        if earning.currency == "RUB":
            cur = Decimal(referrer.ref_balance_rub or 0) - Decimal(earning.amount)
            referrer.ref_balance_rub = cur if cur > 0 else Decimal(0)
        else:
            cur = Decimal(referrer.ref_balance_usdt or 0) - Decimal(earning.amount)
            referrer.ref_balance_usdt = cur if cur > 0 else Decimal(0)
    earning.status = "reversed"
    await session.flush()


async def transfer_to_balance(
    session: AsyncSession, user_id: int, currency: str = "RUB",
    merchant_id: int = HEAD_MERCHANT_ID,
) -> tuple[bool, Decimal, Decimal]:
    """Перевести весь реф-баланс на основной. Возвращает (ok, amount, need_min).

    Основной баланс магазина — рублёвый, поэтому перевод идёт с ₽-реф-баланса
    на основной ₽-баланс.
    """
    user = await get_user(session, user_id, merchant_id)
    if user is None:
        return False, Decimal(0), Decimal(0)
    if currency == "USDT":
        bal = Decimal(user.ref_balance_usdt or 0)
        raw = await settings_service.get(session, settings_service.REF_MIN_WD_USDT)
        minv = Decimal(raw) if raw else DEFAULT_MIN_USDT
    else:
        bal = Decimal(user.ref_balance_rub or 0)
        raw = await settings_service.get(session, settings_service.REF_MIN_WD_RUB)
        minv = Decimal(raw) if raw else DEFAULT_MIN_RUB
    if bal < minv or bal <= 0:
        return False, bal, minv
    if currency == "USDT":
        user.ref_balance_usdt = Decimal(0)
    else:
        user.ref_balance_rub = Decimal(0)
    await balance_service.credit(session, user_id, bal, merchant_id=merchant_id)  # основной ₽-баланс
    await session.flush()
    return True, bal, minv


async def stats(
    session: AsyncSession, user_id: int, merchant_id: int = HEAD_MERCHANT_ID
) -> dict:
    invited = int(await session.scalar(
        select(func.count()).select_from(User).where(
            User.referred_by == user_id, User.merchant_id == merchant_id
        )
    ) or 0)
    with_purchase = int(await session.scalar(
        select(func.count(func.distinct(ReferralEarning.referral_id)))
        .where(ReferralEarning.referrer_id == user_id,
               ReferralEarning.merchant_id == merchant_id,
               ReferralEarning.status == "credited")
    ) or 0)
    user = await get_user(session, user_id, merchant_id)
    return {
        "invited": invited,
        "with_purchase": with_purchase,
        "bal_usdt": Decimal(user.ref_balance_usdt or 0) if user else Decimal(0),
        "earned_usdt": Decimal(user.ref_earned_usdt or 0) if user else Decimal(0),
        "bal_rub": Decimal(user.ref_balance_rub or 0) if user else Decimal(0),
        "earned_rub": Decimal(user.ref_earned_rub or 0) if user else Decimal(0),
    }


async def terms_text(session: AsyncSession) -> str:
    return await settings_service.get(session, settings_service.REF_TERMS, DEFAULT_TERMS)


async def admin_stats(session: AsyncSession) -> dict:
    total_invited = int(await session.scalar(
        select(func.count()).select_from(User).where(User.referred_by.isnot(None))
    ) or 0)
    total_referrers = int(await session.scalar(
        select(func.count(func.distinct(ReferralEarning.referral_id)))
        .where(ReferralEarning.status == "credited")
    ) or 0)
    paid_usdt = await session.scalar(
        select(func.coalesce(func.sum(ReferralEarning.amount), 0)).where(
            ReferralEarning.status == "credited", ReferralEarning.currency == "USDT"
        )
    ) or Decimal(0)
    paid_rub = await session.scalar(
        select(func.coalesce(func.sum(ReferralEarning.amount), 0)).where(
            ReferralEarning.status == "credited", ReferralEarning.currency == "RUB"
        )
    ) or Decimal(0)
    return {
        "total_invited": total_invited,
        "total_referrers": total_referrers,
        "paid_usdt": Decimal(paid_usdt),
        "paid_rub": Decimal(paid_rub),
    }

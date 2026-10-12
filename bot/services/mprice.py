"""Ценовой слой мерчанта (франшиза): цена клиента поверх цены владельца.

На вход — ``owner_price`` (цена головного магазина в ₽, уже округлённая,
например из ``pricing.price_rub_value``). На выход — цена, которую платит
клиент этого мерчанта:

  * есть индивидуальная цена номинала (MerchantVariantPrice):
      - mode="fixed"  → ручная ₽-цена мерчанта;
      - mode="markup" → наценка % поверх цены владельца;
  * иначе — наценка по умолчанию мерчанта (Merchant.default_markup).

Стоп-лосс: цена клиента НИКОГДА не ниже цены владельца (иначе владелец в минусе).
Для головного (merchant_id = 0) цена владельца возвращается без изменений.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, Merchant, MerchantVariantPrice
from .pricing import round_price_rub


def _apply(owner_price: Decimal, mode: Optional[str], markup, fixed, default_markup: Decimal) -> Decimal:
    owner = round_price_rub(Decimal(owner_price))
    if mode == "fixed" and fixed is not None:
        price = round_price_rub(Decimal(fixed))
    elif mode == "markup" and markup is not None:
        price = round_price_rub(owner * (Decimal(1) + Decimal(markup) / Decimal(100)))
    else:
        price = round_price_rub(owner * (Decimal(1) + Decimal(default_markup) / Decimal(100)))
    return price if price > owner else owner  # стоп-лосс


async def default_markup(session: AsyncSession, merchant_id: int) -> Decimal:
    if merchant_id == HEAD_MERCHANT_ID:
        return Decimal(0)
    m = await session.get(Merchant, merchant_id)
    return Decimal(m.default_markup or 0) if m is not None else Decimal(0)


async def get_override(
    session: AsyncSession, merchant_id: int, variant_id: int
) -> Optional[MerchantVariantPrice]:
    return await session.get(MerchantVariantPrice, {"merchant_id": merchant_id, "variant_id": variant_id})


async def client_price(
    session: AsyncSession, merchant_id: int, variant_id: int, owner_price: Decimal
) -> Decimal:
    """Цена для клиента данного мерчанта (для головного — цена владельца)."""
    if merchant_id == HEAD_MERCHANT_ID:
        return round_price_rub(Decimal(owner_price))
    ov = await get_override(session, merchant_id, variant_id)
    dm = await default_markup(session, merchant_id)
    if ov is None:
        return _apply(owner_price, None, None, None, dm)
    return _apply(owner_price, ov.mode, ov.markup_percent, ov.price_rub, dm)


async def markup_price(
    session: AsyncSession, merchant_id: int, variant_id: int, owner_price: Decimal
) -> Decimal:
    """Цена клиента для флоу с динамической ценой (Steam/Stars): только % наценки.

    Цена зависит от суммы, поэтому фикс-режим здесь не применяется — берём
    индивидуальную %-наценку номинала (если задана в режиме markup), иначе
    наценку по умолчанию мерчанта. Стоп-лосс: не ниже цены владельца.
    """
    owner = round_price_rub(Decimal(owner_price))
    if merchant_id == HEAD_MERCHANT_ID:
        return owner
    ov = await get_override(session, merchant_id, variant_id)
    dm = await default_markup(session, merchant_id)
    if ov is not None and ov.mode == "markup" and ov.markup_percent is not None:
        pct = Decimal(ov.markup_percent)
    else:
        pct = dm
    price = round_price_rub(owner * (Decimal(1) + pct / Decimal(100)))
    return price if price > owner else owner


async def client_prices(
    session: AsyncSession, merchant_id: int, owner_prices: Dict[int, Decimal]
) -> Dict[int, Decimal]:
    """Пакетно: {variant_id: owner_price} -> {variant_id: client_price}."""
    if merchant_id == HEAD_MERCHANT_ID:
        return {vid: round_price_rub(Decimal(p)) for vid, p in owner_prices.items()}
    dm = await default_markup(session, merchant_id)
    vids = list(owner_prices.keys())
    overrides = {}
    if vids:
        rows = await session.scalars(
            select(MerchantVariantPrice).where(
                MerchantVariantPrice.merchant_id == merchant_id,
                MerchantVariantPrice.variant_id.in_(vids),
            )
        )
        overrides = {r.variant_id: r for r in rows}
    out: Dict[int, Decimal] = {}
    for vid, owner in owner_prices.items():
        ov = overrides.get(vid)
        if ov is None:
            out[vid] = _apply(owner, None, None, None, dm)
        else:
            out[vid] = _apply(owner, ov.mode, ov.markup_percent, ov.price_rub, dm)
    return out


# ── Баланс заработка мерчанта (маржа) ─────────────────────────────────────────
async def accrue_earnings(session: AsyncSession, merchant_id: int, amount: Decimal) -> None:
    """Начислить маржу на баланс заработка мерчанта (головного — не трогаем)."""
    if merchant_id == HEAD_MERCHANT_ID or Decimal(amount) <= 0:
        return
    m = await session.get(Merchant, merchant_id)
    if m is not None:
        m.earnings_rub = Decimal(m.earnings_rub or 0) + Decimal(amount)
        await session.flush()


async def reverse_earnings(session: AsyncSession, merchant_id: int, amount: Decimal) -> None:
    """Сторнировать маржу (возврат заказа). Баланс не уходит ниже нуля."""
    if merchant_id == HEAD_MERCHANT_ID or Decimal(amount) <= 0:
        return
    m = await session.get(Merchant, merchant_id)
    if m is not None:
        new = Decimal(m.earnings_rub or 0) - Decimal(amount)
        m.earnings_rub = new if new > 0 else Decimal(0)
        await session.flush()


# ── Админка мерчанта (запись) ────────────────────────────────────────────────
async def set_default_markup(session: AsyncSession, merchant_id: int, markup: Decimal) -> None:
    m = await session.get(Merchant, merchant_id)
    if m is not None:
        m.default_markup = Decimal(markup)
        await session.flush()


async def set_markup(session: AsyncSession, merchant_id: int, variant_id: int, markup: Decimal) -> None:
    ov = await get_override(session, merchant_id, variant_id)
    if ov is None:
        ov = MerchantVariantPrice(merchant_id=merchant_id, variant_id=variant_id)
        session.add(ov)
    ov.mode = "markup"
    ov.markup_percent = Decimal(markup)
    ov.price_rub = None
    await session.flush()


async def set_fixed(session: AsyncSession, merchant_id: int, variant_id: int, price_rub: Decimal) -> None:
    ov = await get_override(session, merchant_id, variant_id)
    if ov is None:
        ov = MerchantVariantPrice(merchant_id=merchant_id, variant_id=variant_id)
        session.add(ov)
    ov.mode = "fixed"
    ov.price_rub = Decimal(price_rub)
    ov.markup_percent = None
    await session.flush()


async def clear_override(session: AsyncSession, merchant_id: int, variant_id: int) -> None:
    ov = await get_override(session, merchant_id, variant_id)
    if ov is not None:
        await session.delete(ov)
        await session.flush()

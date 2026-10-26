"""Расчёт цены продажи с учётом наценки.

Приоритет:
  1. ``variant.price_usd`` — фиксированная цена продажи (если задана).
  2. иначе ``variant.cost_usd`` * (1 + markup/100), где markup =
     ``variant.markup_percent`` (если задан), иначе дефолт из конфига.

Все деньги — Decimal, округление до 2 знаков (ROUND_HALF_UP).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Optional

from ..db.models import Variant
from . import rates as rates_service

_CENT = Decimal("0.01")
_DIME = Decimal("0.1")
_ONE = Decimal("1")

# Порог «дешёвых» товаров: ниже него цена округляется до 10 копеек (всегда
# оканчивается на 0: 78.40), от порога и выше — до целого рубля.
CHEAP_THRESHOLD = Decimal("100")


def round_price_rub(value: Decimal) -> Decimal:
    """Округлить рублёвую цену ВВЕРХ: до 10 копеек для <100 ₽, иначе до 1 ₽."""
    v = Decimal(value)
    if v < CHEAP_THRESHOLD:
        return v.quantize(_DIME, rounding=ROUND_CEILING)
    return v.quantize(_ONE, rounding=ROUND_CEILING)


def quantize_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def effective_markup_percent(
    variant: Variant, default_markup_percent: Decimal
) -> Decimal:
    if variant.markup_percent is not None:
        return Decimal(variant.markup_percent)
    return Decimal(default_markup_percent)


def sale_price(variant: Variant, default_markup_percent: Decimal = Decimal("0")) -> Decimal:
    """Вернуть цену продажи для вариации."""
    if variant.price_usd is not None:
        return quantize_money(Decimal(variant.price_usd))

    cost = Decimal(variant.cost_usd or 0)
    markup = effective_markup_percent(variant, default_markup_percent)
    return quantize_money(cost * (Decimal(1) + markup / Decimal(100)))


def _fmt_rub(value: Decimal) -> str:
    d = Decimal(value)
    if d == d.to_integral_value():
        return f"{int(d)}"
    return f"{d:.2f}"


def usd_to_rub(value: Decimal, rate: Optional[Decimal] = None) -> Decimal:
    """Перевести USD в рубли по курсу, округлив ВВЕРХ (до 10 коп. <100 ₽, иначе до 1 ₽)."""
    r = rate if rate is not None else rates_service.get_rate()
    rub = Decimal(value) * Decimal(r)
    return round_price_rub(rub)


def price_rub_value(
    variant: Variant, default_markup_percent: Decimal = Decimal("0")
) -> Decimal:
    """Цена продажи клиенту в рублях (целое число ₽).

    Если у номинала задана ручная «Цена ₽» — берётся она (переопределение),
    иначе цена считается из USDT-цены по курсу (вверх до 1 ₽).
    """
    manual = getattr(variant, "price_rub", None)
    if manual is not None:
        return round_price_rub(manual)
    return usd_to_rub(sale_price(variant, default_markup_percent))


def owner_price_rub(variant: Variant, default_markup_percent: Decimal = Decimal("0")) -> Decimal:
    """Цена владельца (₽) для ЛЮБОГО источника, включая SMM.

    Единый источник правды для бота и сайта. Для SMM (soc-proof) — цена за 1000
    = закуп × (1 + наценка/100); для остальных — ``price_rub_value``.
    """
    if getattr(variant, "source", None) == "socproof":
        from . import smm as smm_service  # ленивый импорт (без циклов)
        mk = variant.markup_percent if variant.markup_percent is not None \
            else default_markup_percent
        return smm_service.sell_per_1000_rub(variant.smm_rate, mk)
    return price_rub_value(variant, default_markup_percent)


def price_label(variant: Variant, default_markup_percent: Decimal = Decimal("0")) -> str:
    """Короткая цена для кнопки/карточки клиента — в рублях."""
    rub = price_rub_value(variant, default_markup_percent)
    return f"{_fmt_rub(rub)} ₽" if rub > 0 else "—"


def margin(variant: Variant, default_markup_percent: Decimal = Decimal("0")) -> Decimal:
    """Абсолютная маржа (цена продажи минус закупка)."""
    return quantize_money(
        sale_price(variant, default_markup_percent) - Decimal(variant.cost_usd or 0)
    )

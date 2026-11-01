"""Пополнение Steam по логину через FazerCards (свободная сумма, 4 валюты).

Ценообразование (Вариант B — от себестоимости):
  себестоимость_USD = сумма_в_валюте / (единиц валюты за 1 USD)
  цена_клиенту_₽    = округл.вверх( себестоимость_USD × курс(USDT→₽) × (1 + наценка%) )

То есть наценка всегда накручивается поверх реальной цены закупки у FazerCards —
маржа = ровно наценка% при любых курсах, ниже себестоимости продать нельзя.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal, ROUND_CEILING
from typing import Dict, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import rates as rates_service
from . import settings as settings_service

# Валюты в порядке показа клиенту.
CURRENCIES = ["RUB", "USD", "KZT", "UAH"]
CURRENCY_LABELS = {
    "RUB": "🇷🇺 RUB",
    "USD": "🇺🇸 USD",
    "KZT": "🇰🇿 KZT",
    "UAH": "🇺🇦 UAH",
}

DEFAULT_MARKUP = Decimal("5")
DEFAULT_MIN_RUB = Decimal("50")
DEFAULT_MAX_RUB = Decimal("50000")

_ONE = Decimal("1")
_RATES_TTL = 600.0  # сек — кэш курсов FazerCards

_rates_cache: Dict[str, Decimal] = {}
_rates_ts: float = 0.0


def _parse_rates(data) -> Dict[str, Decimal]:
    """Нормализовать ответ /steam-topup/rates в {currency: единиц за 1 USD}."""
    out: Dict[str, Decimal] = {"USD": Decimal(1)}
    if not isinstance(data, dict):
        return out
    rates = data.get("rates")
    if not isinstance(rates, dict):
        # иногда курсы лежат прямо в data
        rates = {k: data[k] for k in ("RUB", "KZT", "UAH") if k in data}
    for cur in ("RUB", "KZT", "UAH"):
        v = rates.get(cur) if isinstance(rates, dict) else None
        if v is None and isinstance(rates, dict):
            v = rates.get(cur.lower())
        if v is not None:
            try:
                d = Decimal(str(v))
                if d > 0:
                    out[cur] = d
            except Exception:  # noqa: BLE001
                pass
    return out


async def get_fzr_rates(fzr) -> Dict[str, Decimal]:
    """Курсы FazerCards (единиц валюты за 1 USD), с кэшем ~10 минут."""
    global _rates_cache, _rates_ts
    now = time.time()
    if _rates_cache and (now - _rates_ts) < _RATES_TTL:
        return _rates_cache
    try:
        data = await asyncio.to_thread(fzr.steam_rates)
        parsed = _parse_rates(data)
    except Exception:  # noqa: BLE001 — при сбое используем прошлый кэш
        return _rates_cache or {"USD": Decimal(1)}
    if len(parsed) > 1:
        _rates_cache, _rates_ts = parsed, now
    return parsed


def cost_usd(amount, currency: str, fzr_rates: Dict[str, Decimal]) -> Decimal:
    """Себестоимость заказа в USD."""
    rate = fzr_rates.get(str(currency).upper()) or Decimal(1)
    return Decimal(amount) / Decimal(rate)


def price_rub(amount, currency, fzr_rates, *, markup, our_rate=None) -> Decimal:
    """Цена клиенту в ₽ (целое, округление вверх)."""
    r = our_rate if our_rate is not None else rates_service.get_rate()
    c = cost_usd(amount, currency, fzr_rates)
    rub = c * Decimal(r) * (Decimal(1) + Decimal(markup) / Decimal(100))
    return rub.quantize(_ONE, rounding=ROUND_CEILING)


def rate_per_unit(currency, fzr_rates, *, markup, our_rate=None) -> Decimal:
    """Эффективный курс: сколько ₽ клиент платит за 1 единицу валюты пополнения."""
    r = our_rate if our_rate is not None else rates_service.get_rate()
    c = cost_usd(1, currency, fzr_rates)   # 1 / курс_FazerCard
    return c * Decimal(r) * (Decimal(1) + Decimal(markup) / Decimal(100))


def _dec(raw, default: Decimal) -> Decimal:
    try:
        return Decimal(str(raw)) if raw is not None else default
    except Exception:  # noqa: BLE001
        return default


async def get_markup(session: AsyncSession) -> Decimal:
    return _dec(await settings_service.get(session, settings_service.STEAM_MARKUP), DEFAULT_MARKUP)


async def get_limits(session: AsyncSession) -> Tuple[Decimal, Decimal]:
    lo = _dec(await settings_service.get(session, settings_service.STEAM_MIN_RUB), DEFAULT_MIN_RUB)
    hi = _dec(await settings_service.get(session, settings_service.STEAM_MAX_RUB), DEFAULT_MAX_RUB)
    return lo, hi

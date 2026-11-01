"""Telegram Звёзды через FazerCards — свободный выбор количества, умная цена.

Клиент сам вводит количество звёзд (от min до max по живой котировке, 50–10000).
Цена считается от себестоимости (как пополнение Steam):

  себестоимость_USD = price_per_star × количество
  цена_клиенту_₽    = округл.( себестоимость_USD × курс(USDT→₽) × (1 + наценка%) )

Наценка всегда поверх реальной цены закупки → маржа = ровно наценка% при любом
курсе. Котировка FazerCards (price_per_star, min/max) кэшируется ~10 минут.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from . import rates as rates_service
from . import settings as settings_service
from .pricing import round_price_rub

DEFAULT_MARKUP = Decimal("10")
# Границы по умолчанию (если котировка недоступна) — совпадают с лимитами API.
DEFAULT_MIN_QTY = 50
DEFAULT_MAX_QTY = 10000
# Запасная себестоимость за 1 звезду (USD), если котировка недоступна.
_FALLBACK_PPS = Decimal("0.0152600")

_QUOTE_TTL = 600.0  # сек — кэш котировки FazerCards

_quote_cache: Tuple[Decimal, int, int] | None = None
_quote_ts: float = 0.0


def _to_int(raw, default: int) -> int:
    try:
        v = int(Decimal(str(raw)))
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def _parse_quote(data) -> Tuple[Decimal, int, int]:
    """Нормализовать ответ /telegram/stars → (price_per_star, min, max)."""
    pps, lo, hi = _FALLBACK_PPS, DEFAULT_MIN_QTY, DEFAULT_MAX_QTY
    if isinstance(data, dict):
        try:
            p = Decimal(str(data.get("price_per_star")))
            if p > 0:
                pps = p
        except Exception:  # noqa: BLE001
            pass
        lo = _to_int(data.get("min_amount"), DEFAULT_MIN_QTY)
        hi = _to_int(data.get("max_amount"), DEFAULT_MAX_QTY)
    return pps, lo, hi


async def get_quote(fzr) -> Tuple[Decimal, int, int]:
    """Котировка звёзд (price_per_star USD, min, max), с кэшем ~10 минут."""
    global _quote_cache, _quote_ts
    now = time.time()
    if _quote_cache and (now - _quote_ts) < _QUOTE_TTL:
        return _quote_cache
    try:
        data = await asyncio.to_thread(fzr.telegram_stars_quote)
        parsed = _parse_quote(data)
    except Exception:  # noqa: BLE001 — при сбое используем прошлый кэш/дефолт
        return _quote_cache or (_FALLBACK_PPS, DEFAULT_MIN_QTY, DEFAULT_MAX_QTY)
    _quote_cache, _quote_ts = parsed, now
    return parsed


def cost_usd(quantity: int, price_per_star: Decimal) -> Decimal:
    """Себестоимость заказа в USD."""
    return Decimal(quantity) * Decimal(price_per_star)


def price_rub(quantity: int, price_per_star: Decimal, *, markup, our_rate=None) -> Decimal:
    """Цена клиенту в ₽ (умное округление вверх: 10 копеек <100 ₽, иначе 1 ₽)."""
    r = our_rate if our_rate is not None else rates_service.get_rate()
    c = cost_usd(quantity, price_per_star)
    rub = c * Decimal(r) * (Decimal(1) + Decimal(markup) / Decimal(100))
    return round_price_rub(rub)


def _dec(raw, default: Decimal) -> Decimal:
    try:
        return Decimal(str(raw)) if raw is not None else default
    except Exception:  # noqa: BLE001
        return default


async def get_markup(session: AsyncSession) -> Decimal:
    return _dec(await settings_service.get(session, settings_service.STARS_MARKUP), DEFAULT_MARKUP)


async def get_price_per_star(session: AsyncSession):
    """₽ за 1 звезду, заданная владельцем (или None → считать по котировке+наценке)."""
    raw = await settings_service.get(session, settings_service.STARS_PRICE_PER_STAR)
    if not raw:
        return None
    try:
        v = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
    return v if v > 0 else None


def owner_price(qty: int, *, per_star=None, pps=None, markup=Decimal(0), our_rate=None) -> Decimal:
    """Цена владельца (₽). Если задана цена за звезду — кол-во × цена; иначе по котировке."""
    if per_star is not None:
        return round_price_rub(Decimal(qty) * Decimal(per_star))
    return price_rub(qty, pps, markup=markup, our_rate=our_rate)


async def get_limits(session: AsyncSession, *, api_min: int, api_max: int) -> Tuple[int, int]:
    """Границы количества: переопределение из админки, иначе — из котировки API."""
    lo_raw = await settings_service.get(session, settings_service.STARS_MIN_QTY)
    hi_raw = await settings_service.get(session, settings_service.STARS_MAX_QTY)
    lo = _to_int(lo_raw, api_min) if lo_raw else api_min
    hi = _to_int(hi_raw, api_max) if hi_raw else api_max
    # Никогда не выходим за жёсткие лимиты поставщика.
    lo = max(lo, api_min)
    hi = min(hi, api_max)
    if lo > hi:
        lo, hi = api_min, api_max
    return lo, hi

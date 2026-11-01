"""Глобальные настройки магазина (таблица settings, ключ-значение).

Используются для раздела «Текущие заказы»: переключатель автовозврата и
редактируемые тексты, отправляемые покупателю при возврате/отмене.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Setting

# Ключи настроек.
AUTO_REFUND = "orders_auto_refund"   # "1"/"0" — автовозврат денег при провале
REFUND_TEXT = "orders_refund_text"   # текст покупателю при автовозврате
CANCEL_TEXT = "orders_cancel_text"   # текст покупателю при ручной отмене

# Курс USDT→₽ (рубли за 1 USDT). Задаётся администратором.
USDT_RATE = "usdt_rate"

# VPN (VPNresellers): список server_id через запятую + протокол.
VPN_SERVER_IDS = "vpn_server_ids"
VPN_PROTOCOL = "vpn_protocol"  # пока vless

# Пополнение Steam по логину (FazerCards)
STEAM_MARKUP = "steam_markup_percent"   # наценка %, напр. "5"
STEAM_MIN_RUB = "steam_min_rub"         # мин. сумма заказа клиенту, ₽
STEAM_MAX_RUB = "steam_max_rub"         # макс. сумма заказа клиенту, ₽

# Telegram Звёзды (FazerCards) — свободный выбор количества, умная цена
STARS_MARKUP = "stars_markup_percent"   # наценка %, напр. "10"
STARS_MIN_QTY = "stars_min_qty"         # мин. кол-во звёзд (переопределение API)
STARS_MAX_QTY = "stars_max_qty"         # макс. кол-во звёзд (переопределение API)
STARS_PRICE_PER_STAR = "stars_price_per_star"  # ₽ за 1 звезду; задано → цена = кол-во × это

# Реферальная программа
REF_ENABLED = "ref_enabled"              # "1"/"0"
REF_PERCENT = "ref_percent"              # процент с покупок, напр. "5"
REF_MIN_WD_USDT = "ref_min_withdraw_usdt"  # минимум перевода, USDT
REF_MIN_WD_RUB = "ref_min_withdraw_rub"    # минимум перевода, RUB
REF_TERMS = "ref_terms_text"             # текст условий


async def get(session: AsyncSession, key: str, default: Optional[str] = None) -> Optional[str]:
    row = await session.get(Setting, key)
    return row.value if row is not None and row.value is not None else default


async def set(session: AsyncSession, key: str, value: Optional[str]) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def get_bool(session: AsyncSession, key: str, default: bool = True) -> bool:
    v = await get(session, key)
    if v is None:
        return default
    return v.strip() in ("1", "true", "yes", "on")


def fmt(template: str, *, ref: str = "", amount: str = "") -> str:
    """Безопасная подстановка {ref}/{amount} без падения на прочих скобках."""
    return template.replace("{ref}", ref).replace("{amount}", amount)

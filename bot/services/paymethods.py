"""Способы оплаты пополнения баланса: вкл/выкл и текст кнопок (админка).

Три кнопки на экране пополнения:
  * ``card`` — Банковская карта (провайдер PayHot);
  * ``sbp``  — СБП (провайдер Paydex);
  * ``usdt`` — USDT-крипта (провайдер BoltUtil, свой флоу выбора сети).

Состояние (вкл/выкл + свой текст кнопки + заголовок экрана) хранится в
таблице ``settings`` (ключ-значение). Эмодзи кнопок задаются в ``keyboards``.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from . import settings as settings_service

# Порядок = порядок показа кнопок клиенту.
# ``sbp_ph`` — резервная СБП через PayHot (по умолчанию скрыта).
METHOD_IDS: List[str] = ["card", "sbp", "sbp_ph", "usdt"]

PROVIDER_OF = {"card": "payhot", "sbp": "paydex", "sbp_ph": "payhot", "usdt": "bolt"}

# Код способа, который уходит провайдеру (у PayHot и Paydex СБП = "sbp").
CODE_OF = {"card": "card", "sbp": "sbp", "sbp_ph": "sbp", "usdt": "usdt"}

DEFAULT_LABEL = {
    "card": "Банковская карта",
    "sbp": "СБП",
    "sbp_ph": "СБП (резерв)",
    "usdt": "USDT (крипта)",
}

# Видимость по умолчанию (резервная СБП PayHot скрыта, пока не включат).
DEFAULT_ENABLED = {"sbp_ph": False}

# Человеческие названия для админки.
TITLE = {
    "card": "💳 Банковская карта (PayHot)",
    "sbp": "⚡ СБП (Paydex)",
    "sbp_ph": "⚡ СБП — резерв (PayHot)",
    "usdt": "₮ USDT-крипта (BoltUtil)",
}

HEADING_KEY = "pay_heading"


def method_code(mid: str) -> str:
    """Код способа для провайдера (например, и sbp, и sbp_ph → 'sbp')."""
    return CODE_OF.get(mid, mid)


def _enabled_key(mid: str) -> str:
    return f"pay_{mid}_enabled"


def _label_key(mid: str) -> str:
    return f"pay_{mid}_label"


async def is_enabled(session: AsyncSession, mid: str) -> bool:
    default = DEFAULT_ENABLED.get(mid, True)
    return await settings_service.get_bool(session, _enabled_key(mid), default=default)


async def set_enabled(session: AsyncSession, mid: str, value: bool) -> None:
    await settings_service.set(session, _enabled_key(mid), "1" if value else "0")


async def label(session: AsyncSession, mid: str) -> str:
    v = await settings_service.get(session, _label_key(mid))
    return v if v else DEFAULT_LABEL.get(mid, mid)


async def set_label(session: AsyncSession, mid: str, value: Optional[str]) -> None:
    """Задать свой текст кнопки; ``None`` — вернуть текст по умолчанию."""
    await settings_service.set(session, _label_key(mid), value)


async def heading(session: AsyncSession) -> str:
    v = await settings_service.get(session, HEADING_KEY)
    if v:
        return v
    from .. import texts
    return texts.TOPUP_CHOOSE_PAYMENT


async def set_heading(session: AsyncSession, value: Optional[str]) -> None:
    await settings_service.set(session, HEADING_KEY, value)


async def list_methods(session: AsyncSession) -> List[dict]:
    """Все способы с их состоянием (для админки и для клиентской клавиатуры)."""
    out: List[dict] = []
    for mid in METHOD_IDS:
        out.append(
            {
                "id": mid,
                "provider": PROVIDER_OF[mid],
                "enabled": await is_enabled(session, mid),
                "label": await label(session, mid),
            }
        )
    return out


async def client_methods(session: AsyncSession, *, available: dict) -> List[dict]:
    """Способы для показа клиенту: включены в админке И провайдер доступен.

    ``available`` — карта provider_name -> bool (настроен ли провайдер).
    """
    methods = await list_methods(session)
    return [m for m in methods if m["enabled"] and available.get(m["provider"], False)]

"""История заказов аккаунта для сайта (список + детали с выдачей).

Показываем заказы кошелька аккаунта (merchant_id=0) — включая сделанные в боте
тем же Telegram-пользователем (кошелёк общий). Выдача: коды из стока/FazerCard,
ссылка VPN, прогресс SMM.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BotConfig
from bot.db.models import (
    HEAD_MERCHANT_ID,
    Order,
    OrderStatus,
    Variant,
    VpnSubscription,
    WebAccount,
)
from bot.services import smm as smm_service

# Человекочитаемый статус (данные, без HTML — оформление на фронте).
_STATUS_RU = {
    "completed": "выполнен",
    "created": "создаётся",
    "awaiting_payment": "ожидает оплату",
    "paid": "оплачен",
    "fulfilling": "выдаётся",
    "failed": "ошибка",
    "needs_action": "в обработке",
    "expired": "просрочен",
    "refunded": "возврат",
    "pending": "ожидает",
}

_config: Optional[BotConfig] = None


def _cfg() -> BotConfig:
    global _config
    if _config is None:
        _config = BotConfig.from_env()
    return _config


def _status_str(order: Order) -> str:
    return order.status.value if isinstance(order.status, OrderStatus) else str(order.status)


def _fields(order: Order) -> dict:
    try:
        parsed = json.loads(order.fields_json) if order.fields_json else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _total(order: Order) -> float:
    return float(Decimal(order.price_usd or 0) * (order.quantity or 1))


def _fields_list(order: Order) -> list:
    """fields_json как список {label,value} (Звёзды/Премиум); иначе []."""
    try:
        parsed = json.loads(order.fields_json) if order.fields_json else []
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


async def _titles(session: AsyncSession, variant_ids: List[int]) -> dict:
    if not variant_ids:
        return {}
    res = await session.execute(
        select(Variant.id, Variant.title).where(Variant.id.in_(variant_ids))
    )
    return {vid: title for vid, title in res.all()}


def _base_dto(order: Order, title: str) -> dict:
    status = _status_str(order)
    dto = {
        "id": order.id,
        "title": title or "Товар",
        "quantity": order.quantity or 1,
        "total_rub": _total(order),
        "status": status,
        "status_label": _STATUS_RU.get(status, status),
        "supplier": order.supplier,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }
    # Прогресс SMM для списка.
    if order.supplier == "socproof" and status == OrderStatus.FULFILLING.value:
        prog = _fields(order).get("progress")
        if isinstance(prog, int):
            dto["progress"] = prog
    return dto


async def list_orders(
    session: AsyncSession, account: WebAccount, limit: int = 30
) -> List[dict]:
    wallet = account.wallet_user_id
    if wallet is None:
        return []
    orders = list(
        await session.scalars(
            select(Order)
            .where(Order.user_id == wallet, Order.merchant_id == HEAD_MERCHANT_ID)
            .order_by(Order.id.desc())
            .limit(limit)
        )
    )
    titles = await _titles(session, [o.variant_id for o in orders])
    return [_base_dto(o, titles.get(o.variant_id, "")) for o in orders]


async def order_detail(
    session: AsyncSession, account: WebAccount, order_id: int
) -> Optional[dict]:
    wallet = account.wallet_user_id
    order = await session.get(Order, order_id)
    if (
        order is None
        or wallet is None
        or order.user_id != wallet
        or order.merchant_id != HEAD_MERCHANT_ID
    ):
        return None

    variant = await session.get(Variant, order.variant_id)
    source = (variant.source if variant else None) or "stock"
    dto = _base_dto(order, variant.title if variant else "")
    status = dto["status"]

    if source == "socproof":
        f = _fields(order)
        qty = f.get("quantity") or order.quantity
        dto["smm"] = {
            "link": f.get("link"),
            "quantity": qty,
            "status_label": smm_service.status_label(f.get("status")),
            "progress": f.get("progress") if isinstance(f.get("progress"), int) else None,
            "done": (qty - f["remains"]) if isinstance(f.get("remains"), int)
                    and isinstance(qty, int) else None,
        }
        if status == OrderStatus.REFUNDED.value:
            dto["note"] = "Возврат — деньги возвращены на баланс"
        elif (status == OrderStatus.COMPLETED.value and order.fail_reason
              and "частично" in order.fail_reason):
            dto["note"] = "Частично выполнен — часть возвращена на баланс"
    elif source == "vpnresellers":
        dto["vpn_link"] = await _vpn_link(session, order)
    else:
        # Звёзды/Премиум (FazerCard): получатель хранится списком {label,value}.
        info = _fields_list(order)
        if info:
            dto["info"] = info
        if status == OrderStatus.COMPLETED.value and order.delivery_code:
            dto["codes"] = order.delivery_code.splitlines()

    if order.fail_reason and "note" not in dto:
        dto["fail_reason"] = order.fail_reason
    return dto


async def _vpn_link(session: AsyncSession, order: Order) -> Optional[str]:
    sub = await session.scalar(
        select(VpnSubscription).where(VpnSubscription.order_id == order.id)
    )
    if sub is None:
        return None
    base = (_cfg().public_base_url or "").rstrip("/")
    return f"{base}/sub/{sub.token}" if base else f"/sub/{sub.token}"

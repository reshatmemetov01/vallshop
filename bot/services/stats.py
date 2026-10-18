"""Статистика магазина: пользователи, продажи, выручка, прибыль.

Продажей считаем завершённый заказ (OrderStatus.COMPLETED). Прибыль =
выручка (price_usd * qty) − себестоимость (variant.cost_usd * qty).
Все суммы в USDT. Время — UTC (как хранится created_at).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, Order, OrderStatus, User, Variant
from . import rates as rates_service

# Часовой пояс магазина по умолчанию (дни/«сегодня» считаются по нему). В БД
# время — UTC, поэтому границы периодов переводим из местного времени в UTC.
DEFAULT_TZ = "Europe/Moscow"


def _tz(tz: Optional[str]):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 — неизвестная зона → UTC
        return timezone.utc


def _to_utc_naive(dt_local: datetime) -> datetime:
    """Локальное (aware) время → наивное UTC (как хранится created_at в БД)."""
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def day_bounds(d: datetime, tz: Optional[str] = None) -> Tuple[datetime, datetime]:
    """Границы суток [00:00, 24:00) указанной даты в местном часовом поясе → UTC.

    ``d`` трактуется как календарная дата в часовом поясе магазина.
    """
    z = _tz(tz)
    start_local = datetime(d.year, d.month, d.day, tzinfo=z)
    end_local = start_local + timedelta(days=1)
    return _to_utc_naive(start_local), _to_utc_naive(end_local)


def preset_range(
    preset: str, tz: Optional[str] = None
) -> Tuple[Optional[datetime], datetime, str]:
    """Вернуть (start|None, end, подпись) для пресета. Границы — в UTC, но дни
    отсчитываются по местному часовому поясу магазина (``tz``)."""
    z = _tz(tz)
    now_local = datetime.now(z)
    now_utc = _to_utc_naive(now_local)
    if preset == "today":
        start, _ = day_bounds(now_local, tz)
        return start, now_utc, "Сегодня"
    if preset == "7d":
        return now_utc - timedelta(days=7), now_utc, "7 дней"
    if preset == "30d":
        return now_utc - timedelta(days=30), now_utc, "30 дней"
    if preset == "month":
        start_local = datetime(now_local.year, now_local.month, 1, tzinfo=z)
        return _to_utc_naive(start_local), now_utc, "Этот месяц"
    # all
    return None, now_utc, "Всё время"


async def users_stats(
    session: AsyncSession, start: Optional[datetime], end: datetime,
    merchant_id: Optional[int] = None,
) -> dict:
    total_stmt = select(func.count()).select_from(User)
    stmt = select(func.count()).select_from(User).where(User.created_at < end)
    if merchant_id is not None:
        total_stmt = total_stmt.where(User.merchant_id == merchant_id)
        stmt = stmt.where(User.merchant_id == merchant_id)
    if start is not None:
        stmt = stmt.where(User.created_at >= start)
    total = await session.scalar(total_stmt) or 0
    new = await session.scalar(stmt) or 0
    return {"total": int(total), "new": int(new)}


async def activity_stats(
    session: AsyncSession, merchant_id: Optional[int] = None
) -> dict:
    """Активные/неактивные клиенты (в разрезе мерчанта).

    База — клиенты, НЕ забаненные админом (именно им уходит рассылка).
    Активные — достижимы (``is_active``); неактивные — заблокировали бота.
    """
    base = select(func.count()).select_from(User).where(User.is_blocked.is_(False))
    if merchant_id is not None:
        base = base.where(User.merchant_id == merchant_id)
    total = await session.scalar(base) or 0
    active = await session.scalar(base.where(User.is_active.is_(True))) or 0
    return {
        "total": int(total),
        "active": int(active),
        "inactive": int(total) - int(active),
    }


async def total_balance(
    session: AsyncSession, merchant_id: Optional[int] = None
) -> Decimal:
    """Суммарный баланс клиентов (₽, текущий снимок — не зависит от периода)."""
    stmt = select(func.coalesce(func.sum(User.balance), 0))
    if merchant_id is not None:
        stmt = stmt.where(User.merchant_id == merchant_id)
    total = await session.scalar(stmt)
    return Decimal(total or 0)


async def sales_stats(
    session: AsyncSession, start: Optional[datetime], end: datetime,
    merchant_id: Optional[int] = None,
) -> dict:
    conds = [Order.status == OrderStatus.COMPLETED, Order.created_at < end]
    if start is not None:
        conds.append(Order.created_at >= start)
    if merchant_id is not None:
        conds.append(Order.merchant_id == merchant_id)
    row = (
        await session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.quantity), 0),
                func.coalesce(func.sum(Order.price_usd * Order.quantity), 0),
                func.coalesce(
                    func.sum(
                        func.coalesce(Order.cost_usd, Variant.cost_usd * Order.quantity)
                    ),
                    0,
                ),
                func.coalesce(func.sum(Order.merchant_margin), 0),
            )
            .select_from(Order)
            .join(Variant, Variant.id == Order.variant_id)
            .where(*conds)
        )
    ).one()
    orders, items, revenue, cost_usd, margin = row
    # Выручка (списанное с клиентов) уже в рублях.
    revenue = Decimal(revenue or 0)
    cost_usd = Decimal(cost_usd or 0)
    margin = Decimal(margin or 0)
    rate = rates_service.get_rate()
    if merchant_id is not None and merchant_id != HEAD_MERCHANT_ID:
        # Франшиза: себестоимость = цена владельца (выручка − её маржа),
        # прибыль = накопленная маржа франшизы.
        cost_rub = revenue - margin
        profit = margin
        cost_usd = Decimal(0)  # у франшизы закуп поставщика не показываем
    else:
        # Головной/платформа: себестоимость = закуп поставщика (USDT) по курсу.
        cost_rub = cost_usd * rate
        profit = revenue - cost_rub
    return {
        "orders": int(orders or 0),
        "items": int(items or 0),
        "revenue": revenue,          # ₽
        "cost": cost_rub,            # ₽
        "cost_usd": cost_usd,        # USDT (справочно; 0 для франшизы)
        "profit": profit,            # ₽
    }

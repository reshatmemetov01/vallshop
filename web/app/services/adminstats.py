"""Статистика для админ-панели: раздельно бот и сайт, посещения, пользователи.

БД общая с ботом. Переиспользует bot.services.stats. Суммы — в ₽ (магазин
работает в рублях; Order.price_usd/TopUp.amount_usd хранят ₽ после миграции).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    ActivationRequest,
    HEAD_MERCHANT_ID,
    Order,
    OrderStatus,
    SiteVisit,
    TopUp,
    TopUpStatus,
    User,
    WebAccount,
)
from bot.services import stats as bot_stats

PRESETS = ("today", "7d", "30d", "all")


async def _web_wallet_ids(session: AsyncSession) -> set[int]:
    rows = await session.scalars(
        select(WebAccount.wallet_user_id).where(WebAccount.wallet_user_id.is_not(None))
    )
    return {int(x) for x in rows if x is not None}


async def _orders_sum(session: AsyncSession, start, end, *, in_ids=None, not_in_ids=None):
    """(кол-во, выручка ₽) завершённых заказов за период с фильтром по user_id."""
    conds = [Order.status == OrderStatus.COMPLETED, Order.created_at < end,
             Order.merchant_id == HEAD_MERCHANT_ID]
    if start is not None:
        conds.append(Order.created_at >= start)
    if in_ids is not None:
        if not in_ids:
            return 0, Decimal(0)
        conds.append(Order.user_id.in_(in_ids))
    if not_in_ids:
        conds.append(Order.user_id.not_in(not_in_ids))
    row = (await session.execute(
        select(func.count(Order.id),
               func.coalesce(func.sum(Order.price_usd * Order.quantity), 0))
        .where(*conds)
    )).one()
    return int(row[0] or 0), Decimal(row[1] or 0)


async def bot_block(session: AsyncSession, preset: str = "7d") -> dict:
    start, end, label = bot_stats.preset_range(preset)
    web_ids = await _web_wallet_ids(session)
    users = await bot_stats.users_stats(session, start, end, HEAD_MERCHANT_ID)
    activity = await bot_stats.activity_stats(session, HEAD_MERCHANT_ID)
    balance = await bot_stats.total_balance(session, HEAD_MERCHANT_ID)
    # Заказы бота — всё, что НЕ привязано к веб-кошелькам.
    orders_n, revenue = await _orders_sum(session, start, end, not_in_ids=web_ids)
    return {
        "preset": preset, "label": label,
        "users_total": users["total"], "users_new": users["new"],
        "active": activity["active"], "inactive": activity["inactive"],
        "balance_rub": float(balance),
        "orders": orders_n, "revenue_rub": float(revenue),
    }


async def site_block(session: AsyncSession, preset: str = "7d") -> dict:
    start, end, label = bot_stats.preset_range(preset)
    web_ids = await _web_wallet_ids(session)

    acc_total = await session.scalar(select(func.count()).select_from(WebAccount)) or 0
    acc_new_stmt = select(func.count()).select_from(WebAccount).where(WebAccount.created_at < end)
    if start is not None:
        acc_new_stmt = acc_new_stmt.where(WebAccount.created_at >= start)
    acc_new = await session.scalar(acc_new_stmt) or 0

    orders_n, revenue = await _orders_sum(session, start, end, in_ids=web_ids)

    # Пополнения сайта (оплаченные) за период.
    tconds = [TopUp.status == TopUpStatus.PAID, TopUp.created_at < end,
              TopUp.merchant_id == HEAD_MERCHANT_ID]
    if start is not None:
        tconds.append(TopUp.created_at >= start)
    if web_ids:
        tconds.append(TopUp.user_id.in_(web_ids))
        topups_row = (await session.execute(
            select(func.count(TopUp.id), func.coalesce(func.sum(TopUp.amount_usd), 0)).where(*tconds)
        )).one()
        topups_n, topups_sum = int(topups_row[0] or 0), Decimal(topups_row[1] or 0)
    else:
        topups_n, topups_sum = 0, Decimal(0)

    # Активации кодов с сайта за период.
    aconds = [ActivationRequest.web_account_id.is_not(None), ActivationRequest.created_at < end]
    if start is not None:
        aconds.append(ActivationRequest.created_at >= start)
    acts_n = await session.scalar(
        select(func.count()).select_from(ActivationRequest).where(*aconds)
    ) or 0

    return {
        "preset": preset, "label": label,
        "accounts_total": int(acc_total), "accounts_new": int(acc_new),
        "orders": orders_n, "revenue_rub": float(revenue),
        "topups": topups_n, "topups_rub": float(topups_sum),
        "activations": int(acts_n),
    }


async def visits_summary(session: AsyncSession) -> dict:
    today = _today_str()
    d7 = _day_str(datetime.now(timezone.utc) - timedelta(days=6))

    async def agg(where=None):
        stmt = select(func.coalesce(func.sum(SiteVisit.hits), 0), func.count(SiteVisit.id))
        if where is not None:
            stmt = stmt.where(where)
        row = (await session.execute(stmt)).one()
        return int(row[0] or 0), int(row[1] or 0)

    hits_today, uniq_today = await agg(SiteVisit.day == today)
    hits_7d, uniq_7d = await agg(SiteVisit.day >= d7)
    hits_all, uniq_all = await agg()
    return {
        "today": {"hits": hits_today, "unique": uniq_today},
        "d7": {"hits": hits_7d, "unique": uniq_7d},
        "all": {"hits": hits_all, "unique": uniq_all},
    }


async def visits_daily(session: AsyncSession, days: int = 30) -> list:
    since = _day_str(datetime.now(timezone.utc) - timedelta(days=days - 1))
    rows = (await session.execute(
        select(SiteVisit.day,
               func.coalesce(func.sum(SiteVisit.hits), 0),
               func.count(SiteVisit.id))
        .where(SiteVisit.day >= since)
        .group_by(SiteVisit.day)
        .order_by(SiteVisit.day)
    )).all()
    return [{"day": r[0], "hits": int(r[1] or 0), "unique": int(r[2] or 0)} for r in rows]


async def site_users(session: AsyncSession, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    base = select(WebAccount)
    cnt = select(func.count()).select_from(WebAccount)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        base = base.where(WebAccount.email.ilike(like))
        cnt = cnt.where(WebAccount.email.ilike(like))
    total = await session.scalar(cnt) or 0
    rows = await session.scalars(
        base.order_by(WebAccount.id.desc()).limit(min(limit, 200)).offset(max(offset, 0))
    )
    accs = list(rows)
    # Балансы кошельков одним запросом.
    wids = [a.wallet_user_id for a in accs if a.wallet_user_id]
    balances = {}
    if wids:
        brows = await session.execute(
            select(User.id, User.balance).where(User.id.in_(wids), User.merchant_id == HEAD_MERCHANT_ID)
        )
        balances = {int(r[0]): Decimal(r[1] or 0) for r in brows.all()}
    items = [{
        "id": a.id,
        "email": a.email,
        "email_verified": bool(a.email_verified),
        "telegram_linked": a.telegram_user_id is not None,
        "balance_rub": float(balances.get(a.wallet_user_id or 0, 0)),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in accs]
    return {"total": int(total), "items": items}


def _today_str() -> str:
    return _day_str(datetime.now(timezone.utc))


def _day_str(dt: datetime) -> str:
    # День по МСК (как в bot.services.stats.DEFAULT_TZ) — грубо UTC+3.
    return (dt.astimezone(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")

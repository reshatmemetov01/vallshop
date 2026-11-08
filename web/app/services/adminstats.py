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


async def _orders_sum(session: AsyncSession, start, end, *, source=None):
    """(кол-во, выручка ₽) завершённых заказов за период. source: bot|site|None(все).

    Разделение по Order.source — проставляется при оформлении (бот/сайт),
    не зависит от кошелька, поэтому не путает покупки при привязке Telegram.
    """
    conds = [Order.status == OrderStatus.COMPLETED, Order.created_at < end,
             Order.merchant_id == HEAD_MERCHANT_ID]
    if start is not None:
        conds.append(Order.created_at >= start)
    if source is not None:
        conds.append(Order.source == source)
    row = (await session.execute(
        select(func.count(Order.id),
               func.coalesce(func.sum(Order.price_usd * Order.quantity), 0))
        .where(*conds)
    )).one()
    return int(row[0] or 0), Decimal(row[1] or 0)


async def _topups_sum(session: AsyncSession, start, end, *, source=None):
    """(кол-во, сумма ₽) оплаченных пополнений за период. source: bot|site|None."""
    conds = [TopUp.status == TopUpStatus.PAID, TopUp.created_at < end,
             TopUp.merchant_id == HEAD_MERCHANT_ID]
    if start is not None:
        conds.append(TopUp.created_at >= start)
    if source is not None:
        conds.append(TopUp.source == source)
    row = (await session.execute(
        select(func.count(TopUp.id), func.coalesce(func.sum(TopUp.amount_usd), 0)).where(*conds)
    )).one()
    return int(row[0] or 0), Decimal(row[1] or 0)


async def bot_block(session: AsyncSession, preset: str = "7d") -> dict:
    start, end, label = bot_stats.preset_range(preset)
    users = await bot_stats.users_stats(session, start, end, HEAD_MERCHANT_ID)
    activity = await bot_stats.activity_stats(session, HEAD_MERCHANT_ID)
    balance = await bot_stats.total_balance(session, HEAD_MERCHANT_ID)
    orders_n, revenue = await _orders_sum(session, start, end, source="bot")
    tn, ts = await _topups_sum(session, start, end, source="bot")
    franchise = await bot_stats.franchise_users_count(session)
    return {
        "preset": preset, "label": label,
        "users_total": users["total"], "users_new": users["new"],
        "franchise_users": franchise,
        "active": activity["active"], "inactive": activity["inactive"],
        "balance_rub": float(balance),
        "orders": orders_n, "revenue_rub": float(revenue),
        "topups": tn, "topups_rub": float(ts),
    }


async def site_block(session: AsyncSession, preset: str = "7d") -> dict:
    start, end, label = bot_stats.preset_range(preset)

    acc_total = await session.scalar(select(func.count()).select_from(WebAccount)) or 0
    acc_new_stmt = select(func.count()).select_from(WebAccount).where(WebAccount.created_at < end)
    if start is not None:
        acc_new_stmt = acc_new_stmt.where(WebAccount.created_at >= start)
    acc_new = await session.scalar(acc_new_stmt) or 0

    orders_n, revenue = await _orders_sum(session, start, end, source="site")
    topups_n, topups_sum = await _topups_sum(session, start, end, source="site")

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


async def total_block(session: AsyncSession, preset: str = "7d") -> dict:
    """Общая статистика (бот + сайт вместе) за период."""
    start, end, label = bot_stats.preset_range(preset)
    orders_n, revenue = await _orders_sum(session, start, end, source=None)
    topups_n, topups_sum = await _topups_sum(session, start, end, source=None)
    users = await bot_stats.users_stats(session, start, end, HEAD_MERCHANT_ID)
    accounts = await session.scalar(select(func.count()).select_from(WebAccount)) or 0
    balance = await bot_stats.total_balance(session, HEAD_MERCHANT_ID)
    return {
        "preset": preset, "label": label,
        "users_total": users["total"], "accounts_total": int(accounts),
        "orders": orders_n, "revenue_rub": float(revenue),
        "topups": topups_n, "topups_rub": float(topups_sum),
        "balance_rub": float(balance),
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


async def reset_visits(session: AsyncSession) -> int:
    """Обнулить статистику посещений: удалить все записи SiteVisit."""
    from sqlalchemy import delete
    n = await session.scalar(select(func.count(SiteVisit.id))) or 0
    await session.execute(delete(SiteVisit))
    await session.commit()
    return int(n)


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

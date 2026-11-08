"""Статистика для админ-панели: раздельно бот и сайт, посещения, пользователи.

БД общая с ботом. Переиспользует bot.services.stats. Суммы — в ₽ (магазин
работает в рублях; Order.price_usd/TopUp.amount_usd хранят ₽ после миграции).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    ActivationRequest,
    HEAD_MERCHANT_ID,
    Order,
    OrderStatus,
    Product,
    SiteVisit,
    TopUp,
    TopUpStatus,
    User,
    Variant,
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
    """Обнулить статистику посещений: удалить агрегат и технический журнал."""
    from sqlalchemy import delete
    from bot.db.models import SiteVisitLog
    n = await session.scalar(select(func.count(SiteVisit.id))) or 0
    await session.execute(delete(SiteVisit))
    await session.execute(delete(SiteVisitLog))
    await session.commit()
    return int(n)


async def recent_visits(session: AsyncSession, limit: int = 100) -> list:
    """Последние засчитанные визиты (IP, браузер, страница, время) — для
    технической страницы «Журнал визитов» в админке."""
    from bot.db.models import SiteVisitLog
    rows = await session.scalars(
        select(SiteVisitLog).order_by(SiteVisitLog.id.desc()).limit(min(max(limit, 1), 500))
    )
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "ip": r.ip,
            "ua": r.user_agent,
            "path": r.path,
            "is_new": bool(r.is_new),
        })
    return out


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


def _status_str(st) -> str:
    return st.value if hasattr(st, "value") else str(st)


async def orders_list(
    session: AsyncSession, q: str = "", status: str = "", source: str = "",
    limit: int = 50, offset: int = 0,
) -> dict:
    """Список заказов (головной магазин) с фильтрами и поиском — для таблицы."""
    conds = [Order.merchant_id == HEAD_MERCHANT_ID]
    if status:
        conds.append(Order.status == status)
    if source in ("bot", "site"):
        conds.append(Order.source == source)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        ors = [
            Order.client_ref.ilike(like),
            Order.supplier_order_id.ilike(like),
            Order.fail_reason.ilike(like),
        ]
        if q.isdigit():
            ors.append(Order.id == int(q))
            ors.append(Order.user_id == int(q))
        conds.append(or_(*ors))

    total = await session.scalar(select(func.count()).select_from(Order).where(*conds)) or 0
    rows = list(await session.scalars(
        select(Order).where(*conds)
        .order_by(Order.id.desc()).limit(min(max(limit, 1), 200)).offset(max(offset, 0))
    ))

    variants = {}
    vids = {r.variant_id for r in rows}
    if vids:
        for v in await session.scalars(select(Variant).where(Variant.id.in_(vids))):
            variants[v.id] = v
    accounts = {}
    uids = {r.user_id for r in rows}
    if uids:
        for a in await session.scalars(select(WebAccount).where(WebAccount.wallet_user_id.in_(uids))):
            accounts[a.wallet_user_id] = a

    items = []
    for r in rows:
        v = variants.get(r.variant_id)
        a = accounts.get(r.user_id)
        items.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "source": r.source,
            "status": _status_str(r.status),
            "item": v.title if v else f"#{r.variant_id}",
            "supplier": r.supplier,
            "quantity": r.quantity,
            "total_rub": float(Decimal(r.price_usd or 0) * (r.quantity or 1)),
            "user_id": r.user_id,
            "email": a.email if a else None,
            "delivered": r.status == OrderStatus.COMPLETED,
            "has_error": bool(r.fail_reason),
        })
    return {"total": int(total), "items": items}


async def order_detail(session: AsyncSession, order_id: int) -> Optional[dict]:
    """Полная карточка одного заказа: товар, поставщик, пользователь, почта,
    телеграм, статус, выдача, ошибка, собранные при покупке данные."""
    o = await session.get(Order, order_id)
    if o is None:
        return None
    v = await session.get(Variant, o.variant_id)
    product = await session.get(Product, v.product_id) if (v and v.product_id) else None
    user = await session.get(User, {"id": o.user_id, "merchant_id": o.merchant_id})
    acc = await session.scalar(select(WebAccount).where(WebAccount.wallet_user_id == o.user_id))

    buyer_fields = None
    if o.fields_json:
        try:
            parsed = json.loads(o.fields_json)
            if isinstance(parsed, dict):
                buyer_fields = {k: parsed[k] for k in parsed if not str(k).startswith("_")}
            else:
                buyer_fields = {"raw": str(parsed)[:500]}
        except (ValueError, TypeError):
            buyer_fields = {"raw": o.fields_json[:500]}

    telegram_id = acc.telegram_user_id if acc else (o.user_id if o.source == "bot" else None)
    total = Decimal(o.price_usd or 0) * (o.quantity or 1)
    return {
        "id": o.id,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
        "source": o.source,
        "status": _status_str(o.status),
        "delivered": o.status == OrderStatus.COMPLETED,
        "client_ref": o.client_ref,
        "quantity": o.quantity,
        "price_rub": float(o.price_usd or 0),
        "total_rub": float(total),
        "cost_usd": float(o.cost_usd) if o.cost_usd is not None else None,
        "merchant_margin_rub": float(o.merchant_margin or 0),
        "supplier": o.supplier,
        "supplier_order_id": o.supplier_order_id,
        "delivery_code": o.delivery_code,
        "fail_reason": o.fail_reason,
        "fields": buyer_fields,
        "item": {
            "variant_id": o.variant_id,
            "variant_title": v.title if v else None,
            "product_title": product.title if product else None,
            "fzr_kind": getattr(v, "fzr_kind", None) if v else None,
            "source": getattr(v, "source", None) if v else None,
        },
        "user": {
            "user_id": o.user_id,
            "username": user.username if user else None,
            "full_name": user.full_name if user else None,
            "balance_rub": float(user.balance) if (user and user.balance is not None) else None,
            "telegram_id": telegram_id,
        },
        "account": ({
            "id": acc.id,
            "email": acc.email,
            "email_verified": bool(acc.email_verified),
            "telegram_user_id": acc.telegram_user_id,
            "created_at": acc.created_at.isoformat() if acc.created_at else None,
        } if acc else None),
    }


def _today_str() -> str:
    return _day_str(datetime.now(timezone.utc))


def _day_str(dt: datetime) -> str:
    # День по МСК (как в bot.services.stats.DEFAULT_TZ) — грубо UTC+3.
    return (dt.astimezone(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")

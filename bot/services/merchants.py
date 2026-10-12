"""Реестр мерчантов-франшиз (Фаза 2): какие боты поднимать в рантайме.

Головной магазин (``HEAD_MERCHANT_ID = 0``) работает на токене из окружения
(``config.bot_token``) и здесь не возвращается. Франшизы — это записи в таблице
``merchants`` со статусом ``active`` и заданным ``bot_token``.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, text

from ..db import Database
from ..db.models import (
    HEAD_MERCHANT_ID,
    Merchant,
    Order,
    Review,
    TopUp,
    User,
)


async def active_merchants(db: Database) -> List[Merchant]:
    """Активные франшизы с заданным токеном бота (без головного)."""
    async with db.session() as s:
        rows = await s.scalars(
            select(Merchant).where(
                Merchant.status == "active",
                Merchant.bot_token.is_not(None),
                Merchant.id != HEAD_MERCHANT_ID,
            )
        )
        return list(rows)


async def get_merchant(db: Database, merchant_id: int) -> Optional[Merchant]:
    async with db.session() as s:
        return await s.get(Merchant, merchant_id)


async def merchant_for_admin(db: Database, admin_user_id: int) -> Optional[Merchant]:
    """Франшиза, которой владеет данный пользователь (одна на аккаунт)."""
    async with db.session() as s:
        return await s.scalar(
            select(Merchant).where(
                Merchant.admin_user_id == admin_user_id,
                Merchant.id != HEAD_MERCHANT_ID,
            )
        )


async def token_in_use(db: Database, bot_token: str) -> bool:
    async with db.session() as s:
        found = await s.scalar(select(Merchant.id).where(Merchant.bot_token == bot_token))
        return found is not None


async def create_merchant(
    db: Database, *, title: str, bot_token: str, bot_username: str,
    admin_user_id: int, admin_username: Optional[str],
) -> Merchant:
    async with db.session() as s:
        m = Merchant(
            title=title, bot_token=bot_token, bot_username=bot_username,
            admin_user_id=admin_user_id, admin_username=admin_username,
            status="active",
        )
        s.add(m)
        await s.commit()
        return m


async def list_all(db: Database) -> List[Merchant]:
    """Все франшизы (без головного), новые сверху."""
    async with db.session() as s:
        return list(await s.scalars(
            select(Merchant).where(Merchant.id != HEAD_MERCHANT_ID).order_by(Merchant.id.desc())
        ))


async def set_status(db: Database, merchant_id: int, status: str) -> None:
    async with db.session() as s:
        m = await s.get(Merchant, merchant_id)
        if m is not None and m.id != HEAD_MERCHANT_ID:
            m.status = status
            await s.commit()


async def merchant_stats(db: Database, merchant_id: int) -> dict:
    async with db.session() as s:
        users = int(await s.scalar(
            select(func.count()).select_from(User).where(User.merchant_id == merchant_id)
        ) or 0)
        orders = int(await s.scalar(
            select(func.count()).select_from(Order).where(Order.merchant_id == merchant_id)
        ) or 0)
    return {"users": users, "orders": orders}


async def delete_merchant(db: Database, merchant_id: int) -> bool:
    """Удалить франшизу и все её данные (клиенты, заказы, пополнения, отзывы и т.п.).

    Головной (0) удалять нельзя. Возвращает True, если удалено.
    """
    if merchant_id == HEAD_MERCHANT_ID:
        return False
    async with db.session() as s:
        m = await s.get(Merchant, merchant_id)
        if m is None:
            return False
        # платежи по заказам этой франшизы (FK order_id)
        await s.execute(text(
            "DELETE FROM payments WHERE order_id IN "
            "(SELECT id FROM orders WHERE merchant_id = :mid)"
        ), {"mid": merchant_id})
        await s.execute(sa_delete(Order).where(Order.merchant_id == merchant_id))
        await s.execute(sa_delete(TopUp).where(TopUp.merchant_id == merchant_id))
        await s.execute(sa_delete(Review).where(Review.merchant_id == merchant_id))
        for tbl in ("referral_earnings", "activation_requests", "vpn_subscriptions"):
            await s.execute(text(f"DELETE FROM {tbl} WHERE merchant_id = :mid"), {"mid": merchant_id})
        await s.execute(sa_delete(User).where(User.merchant_id == merchant_id))
        await s.delete(m)
        await s.commit()
        return True

"""Реестр мерчантов-франшиз (Фаза 2): какие боты поднимать в рантайме.

Головной магазин (``HEAD_MERCHANT_ID = 0``) работает на токене из окружения
(``config.bot_token``) и здесь не возвращается. Франшизы — это записи в таблице
``merchants`` со статусом ``active`` и заданным ``bot_token``.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Merchant


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

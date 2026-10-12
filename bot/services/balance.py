"""Операции с балансом пользователя и пополнениями."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, TopUp, TopUpStatus, User

_CENT = Decimal("0.01")

# Лимиты пополнения — в рублях (баланс рублёвый).
TOPUP_MIN = Decimal("50")
TOPUP_MAX = Decimal("50000")


def _q(v: Decimal) -> Decimal:
    return Decimal(v).quantize(_CENT, rounding=ROUND_HALF_UP)


def new_topup_ref() -> str:
    return "tu-" + uuid.uuid4().hex[:16]


async def get_balance(
    session: AsyncSession, user_id: int, merchant_id: int = HEAD_MERCHANT_ID
) -> Decimal:
    val = await session.scalar(
        select(User.balance).where(
            User.id == user_id, User.merchant_id == merchant_id
        )
    )
    return Decimal(val or 0)


async def credit(
    session: AsyncSession, user_id: int, amount: Decimal,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    """Зачислить сумму на баланс."""
    await session.execute(
        update(User)
        .where(User.id == user_id, User.merchant_id == merchant_id)
        .values(balance=User.balance + _q(amount))
    )
    await session.flush()


async def try_debit(
    session: AsyncSession, user_id: int, amount: Decimal,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> bool:
    """Атомарно списать сумму, только если баланса хватает. Возвращает успех."""
    amount = _q(amount)
    result = await session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.merchant_id == merchant_id,
            User.balance >= amount,
        )
        .values(balance=User.balance - amount)
    )
    await session.flush()
    return result.rowcount == 1


# ── Пополнения ───────────────────────────────────────────────────────────────
async def create_topup(
    session: AsyncSession, *, user_id: int, amount_usd: Decimal,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> TopUp:
    topup = TopUp(
        client_ref=new_topup_ref(),
        user_id=user_id,
        merchant_id=merchant_id,
        amount_usd=_q(amount_usd),
        status=TopUpStatus.PENDING,
    )
    session.add(topup)
    await session.flush()
    return topup


async def get_topup_by_ref(session: AsyncSession, client_ref: str) -> Optional[TopUp]:
    return await session.scalar(select(TopUp).where(TopUp.client_ref == client_ref))


async def list_topups(
    session: AsyncSession, user_id: int, merchant_id: int = HEAD_MERCHANT_ID,
    limit: int = 15,
) -> List[TopUp]:
    return list(
        await session.scalars(
            select(TopUp)
            .where(TopUp.user_id == user_id, TopUp.merchant_id == merchant_id)
            .order_by(TopUp.id.desc())
            .limit(limit)
        )
    )

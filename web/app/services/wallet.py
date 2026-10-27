"""Кошелёк аккаунта сайта: строка User(merchant_id=0) с балансом/заказами."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import HEAD_MERCHANT_ID, User, WebAccount
from bot.services import balance as balance_service

# Синтетический id кошелька для аккаунта БЕЗ Telegram. Telegram-id всегда
# положительные → отрицательные гарантированно не конфликтуют с ними.
_SYNTH_BASE = 2_000_000_000


def synthetic_wallet_id(account_id: int) -> int:
    return -(_SYNTH_BASE + int(account_id))


async def ensure_wallet(session: AsyncSession, account: WebAccount) -> int:
    """Вернуть user_id кошелька, создав пустой User при необходимости."""
    if account.wallet_user_id is None:
        wid = synthetic_wallet_id(account.id)
        account.wallet_user_id = wid
        existing = await session.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
        if existing is None:
            session.add(User(id=wid, merchant_id=HEAD_MERCHANT_ID))
        await session.flush()
    return account.wallet_user_id


async def get_balance(session: AsyncSession, account: WebAccount) -> Decimal:
    if account.wallet_user_id is None:
        return Decimal("0")
    return await balance_service.get_balance(
        session, account.wallet_user_id, HEAD_MERCHANT_ID
    )

"""Связывание аккаунта сайта с Telegram и слияние кошельков.

Правило (выбрано владельцем): при связывании кошелёк Telegram — главный,
балансы складываются, история (заказы/пополнения/подписки/отзывы) объединяется.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    HEAD_MERCHANT_ID,
    ActivationRequest,
    BonusActivation,
    BroadcastClick,
    Order,
    PromoActivation,
    Review,
    TopUp,
    User,
    VpnSubscription,
    WebAccount,
)

# Таблицы с колонкой user_id, которые переносим со старого кошелька на новый.
_REASSIGN = [
    ActivationRequest,
    BonusActivation,
    BroadcastClick,
    Order,
    PromoActivation,
    Review,
    TopUp,
    VpnSubscription,
]


class LinkError(Exception):
    """Ошибка связывания (текст пригоден для показа клиенту)."""


async def _ensure_user(session: AsyncSession, user_id: int) -> User:
    u = await session.get(User, {"id": user_id, "merchant_id": HEAD_MERCHANT_ID})
    if u is None:
        u = User(id=user_id, merchant_id=HEAD_MERCHANT_ID)
        session.add(u)
        await session.flush()
    return u


async def merge_wallets(
    session: AsyncSession, from_id: int, to_id: int, merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    """Слить кошелёк from_id в to_id: сложить баланс, перенести историю, удалить старый."""
    from_id, to_id = int(from_id), int(to_id)
    if from_id == to_id:
        return
    to_user = await _ensure_user(session, to_id)
    from_user = await session.get(User, {"id": from_id, "merchant_id": merchant_id})
    if from_user is None:
        return  # старого кошелька нет — нечего переносить
    to_user.balance = Decimal(to_user.balance or 0) + Decimal(from_user.balance or 0)
    for Model in _REASSIGN:
        cond = [Model.user_id == from_id]
        if hasattr(Model, "merchant_id"):
            cond.append(Model.merchant_id == merchant_id)
        await session.execute(update(Model).where(*cond).values(user_id=to_id))
    await session.delete(from_user)
    await session.flush()


async def by_telegram(session: AsyncSession, tg_id: int) -> Optional[WebAccount]:
    return await session.scalar(
        select(WebAccount).where(WebAccount.telegram_user_id == int(tg_id))
    )


async def login_or_link_telegram(
    session: AsyncSession, tg_id: int, current: Optional[WebAccount] = None
) -> WebAccount:
    """Вход через Telegram или привязка к текущему аккаунту.

    * current=None — вход: вернуть существующий аккаунт этого Telegram, иначе
      создать новый (без email/пароля), кошелёк = Telegram.
    * current задан — привязать Telegram к этому аккаунту: кошелёк Telegram
      становится главным, балансы/история объединяются.
    """
    tg_id = int(tg_id)
    existing = await by_telegram(session, tg_id)

    if current is None:
        if existing is not None:
            return existing
        await _ensure_user(session, tg_id)  # кошелёк Telegram (мог уже быть в боте)
        acc = WebAccount(telegram_user_id=tg_id, wallet_user_id=tg_id)
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        return acc

    # Привязка к текущему аккаунту.
    if existing is not None and existing.id != current.id:
        raise LinkError("Этот Telegram уже привязан к другому аккаунту")
    if current.telegram_user_id == tg_id:
        return current  # уже привязан

    old_wallet = current.wallet_user_id
    if old_wallet is not None and old_wallet != tg_id:
        await merge_wallets(session, old_wallet, tg_id)
    else:
        await _ensure_user(session, tg_id)
    current.telegram_user_id = tg_id
    current.wallet_user_id = tg_id
    await session.commit()
    await session.refresh(current)
    return current

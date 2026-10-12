"""Доступ к пользователям с учётом составного ключа (id, merchant_id).

У таблицы ``users`` ключ составной: один Telegram-пользователь может быть
клиентом у разных мерчантов со своим балансом у каждого. Хелпер скрывает
сборку составного ключа; ``merchant_id`` по умолчанию — головной магазин (0).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, User


async def get_user(
    session: AsyncSession, user_id: int, merchant_id: int = HEAD_MERCHANT_ID
) -> Optional[User]:
    """Вернуть пользователя по (user_id, merchant_id) или None."""
    return await session.get(User, {"id": user_id, "merchant_id": merchant_id})

"""Одноразовые токены для писем (подтверждение email, сброс пароля)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import EmailToken

VERIFY = "verify"
RESET = "reset"

VERIFY_TTL = timedelta(days=2)
RESET_TTL = timedelta(hours=2)


async def issue(
    session: AsyncSession, account_id: int, purpose: str, ttl: timedelta
) -> str:
    """Создать новый токен. Старые неиспользованные того же назначения гасим."""
    await session.execute(
        update(EmailToken)
        .where(
            EmailToken.account_id == account_id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
        .values(used_at=datetime.utcnow())
    )
    token = secrets.token_urlsafe(32)
    session.add(
        EmailToken(
            account_id=account_id,
            purpose=purpose,
            token=token,
            expires_at=datetime.utcnow() + ttl,
        )
    )
    await session.flush()
    return token


async def consume(session: AsyncSession, token: str, purpose: str) -> Optional[int]:
    """Проверить и погасить токен. Возвращает account_id или None."""
    row = await session.scalar(
        select(EmailToken).where(
            EmailToken.token == token, EmailToken.purpose == purpose
        )
    )
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    await session.flush()
    return row.account_id

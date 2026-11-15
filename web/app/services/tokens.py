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

# Подтверждение по 6-значному коду (письмо с кодом вместо ссылки).
CODE_TTL = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5          # после стольких неверных попыток код сгорает


def _gen_code() -> str:
    """Шестизначный код 100000–999999 (без ведущих нулей — удобнее вводить)."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


async def issue_code(
    session: AsyncSession, account_id: int, purpose: str,
    ttl: timedelta = CODE_TTL,
) -> str:
    """Выдать новый код. Старые неиспользованные того же назначения гасим."""
    await session.execute(
        update(EmailToken)
        .where(
            EmailToken.account_id == account_id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
        .values(used_at=datetime.utcnow())
    )
    code = _gen_code()
    # token обязан быть уникальным и непустым — кладём случайную строку,
    # а проверяем именно по полю code (в паре с account_id+purpose).
    session.add(
        EmailToken(
            account_id=account_id,
            purpose=purpose,
            token=secrets.token_urlsafe(24),
            code=code,
            expires_at=datetime.utcnow() + ttl,
        )
    )
    await session.flush()
    return code


async def consume_code(
    session: AsyncSession, account_id: int, purpose: str, code: str
) -> bool:
    """Проверить и погасить код для аккаунта. True — код верный.

    Защита от перебора: считаем неверные попытки активного кода; после
    MAX_CODE_ATTEMPTS код сгорает (нужно запросить новый)."""
    row = await session.scalar(
        select(EmailToken)
        .where(
            EmailToken.account_id == account_id,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
            EmailToken.code.is_not(None),
        )
        .order_by(EmailToken.id.desc())
        .limit(1)
    )
    if row is None or row.expires_at < datetime.utcnow():
        return False
    code = (code or "").strip()
    if (
        len(code) == 6 and code.isdigit()
        and row.code is not None
        and secrets.compare_digest(row.code, code)
    ):
        row.used_at = datetime.utcnow()
        await session.flush()
        return True
    # Неверно — учитываем попытку, после лимита сжигаем код.
    row.attempts = int(row.attempts or 0) + 1
    if row.attempts >= MAX_CODE_ATTEMPTS:
        row.used_at = datetime.utcnow()
    await session.flush()
    return False


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

"""Аккаунты сайта: регистрация, вход, поиск. Кошелёк/связывание — отдельно."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..security import hash_password, verify_password

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 8


class AccountError(Exception):
    """Ошибка регистрации/входа (текст пригоден для показа клиенту)."""


class EmailTaken(AccountError):
    pass


class InvalidCredentials(AccountError):
    pass


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


async def get_by_email(session: AsyncSession, email: str) -> Optional[WebAccount]:
    return await session.scalar(
        select(WebAccount).where(WebAccount.email == _norm_email(email))
    )


async def get_by_id(session: AsyncSession, account_id: int) -> Optional[WebAccount]:
    return await session.get(WebAccount, int(account_id))


async def register(session: AsyncSession, email: str, password: str) -> WebAccount:
    email = _norm_email(email)
    if not _EMAIL_RE.match(email):
        raise AccountError("Некорректный email")
    if len(password or "") < _MIN_PASSWORD:
        raise AccountError(f"Пароль минимум {_MIN_PASSWORD} символов")
    if await get_by_email(session, email) is not None:
        raise EmailTaken("Этот email уже зарегистрирован")
    acc = WebAccount(email=email, password_hash=hash_password(password))
    session.add(acc)
    await session.commit()
    await session.refresh(acc)
    return acc


async def authenticate(session: AsyncSession, email: str, password: str) -> WebAccount:
    acc = await get_by_email(session, email)
    if acc is None or not verify_password(password, acc.password_hash):
        raise InvalidCredentials("Неверный email или пароль")
    if acc.is_blocked:
        raise InvalidCredentials("Аккаунт заблокирован")
    acc.last_login_at = datetime.utcnow()
    await session.commit()
    return acc

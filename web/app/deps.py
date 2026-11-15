"""Зависимости FastAPI: текущий аккаунт по JWT (Bearer-токен)."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from .db import get_session
from .security import decode_token, is_admin_token
from .services import accounts as account_service


async def current_account(
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session),
) -> WebAccount:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Требуется вход")
    account_id = decode_token(authorization.split(" ", 1)[1])
    if account_id is None:
        raise HTTPException(status_code=401, detail="Недействительный токен")
    acc = await account_service.get_by_id(session, account_id)
    if acc is None or acc.is_blocked:
        raise HTTPException(status_code=401, detail="Аккаунт недоступен")
    return acc


async def verified_account(
    acc: WebAccount = Depends(current_account),
) -> WebAccount:
    """Как current_account, но требует подтверждённого email для денежных действий.

    Аккаунты, вошедшие через Telegram (есть telegram_user_id), считаем
    подтверждёнными. Блокируем только email-аккаунт с неподтверждённой почтой.
    """
    if acc.email and not acc.email_verified and acc.telegram_user_id is None:
        raise HTTPException(
            status_code=403,
            detail="Подтвердите email, чтобы совершать покупки и пополнения",
        )
    return acc


async def require_admin(authorization: Optional[str] = Header(None)) -> bool:
    """Доступ только с валидным админ-токеном (панель управления)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Требуется вход в админку")
    if not is_admin_token(authorization.split(" ", 1)[1]):
        raise HTTPException(status_code=401, detail="Недействительный админ-токен")
    return True

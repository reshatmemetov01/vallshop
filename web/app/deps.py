"""Зависимости FastAPI: текущий аккаунт по JWT (Bearer-токен)."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from .db import get_session
from .security import decode_token
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

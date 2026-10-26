"""Аккаунты сайта: регистрация, вход, профиль (JWT)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..db import get_session
from ..deps import current_account
from ..security import create_token
from ..services import accounts as account_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


def _dto(acc: WebAccount) -> dict:
    return {
        "id": acc.id,
        "email": acc.email,
        "email_verified": bool(acc.email_verified),
        "telegram_linked": acc.telegram_user_id is not None,
    }


@router.post("/register")
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        acc = await account_service.register(session, body.email, body.password)
    except account_service.EmailTaken:
        raise HTTPException(status_code=409, detail="Этот email уже зарегистрирован")
    except account_service.AccountError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.post("/login")
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        acc = await account_service.authenticate(session, body.email, body.password)
    except account_service.InvalidCredentials as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.get("/me")
async def me(acc: WebAccount = Depends(current_account)) -> dict:
    return _dto(acc)

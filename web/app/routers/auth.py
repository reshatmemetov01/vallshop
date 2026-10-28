"""Аккаунты сайта: регистрация, вход, профиль (JWT)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..config import settings
from ..db import get_session
from ..deps import current_account
from ..security import create_token, decode_token
from ..services import accounts as account_service
from ..services import linking as linking_service
from ..services import mail_flows as mail_service
from ..services import tokens as token_service
from ..services import wallet as wallet_service
from ..telegram import verify_login_widget

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class TokenIn(BaseModel):
    token: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


@router.get("/config")
async def public_config() -> dict:
    """Публичные параметры для фронта (кнопка Telegram показывается только если задано имя бота)."""
    return {"telegram_bot_username": settings.telegram_bot_username}


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
    # Письмо с подтверждением (если SMTP настроен; вход при этом не блокируется).
    await mail_service.send_verification(session, acc)
    await session.commit()
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.post("/verify")
async def verify_email(body: TokenIn, session: AsyncSession = Depends(get_session)) -> dict:
    acc_id = await token_service.consume(session, body.token, token_service.VERIFY)
    if acc_id is None:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Ссылка недействительна или устарела")
    acc = await account_service.get_by_id(session, acc_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    acc.email_verified = True
    await session.commit()
    return {"ok": True}


@router.post("/resend")
async def resend_verification(
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not acc.email:
        raise HTTPException(status_code=400, detail="К аккаунту не привязан email")
    if acc.email_verified:
        return {"ok": True, "already": True}
    sent = await mail_service.send_verification(session, acc)
    await session.commit()
    return {"ok": True, "sent": sent}


@router.post("/forgot")
async def forgot_password(body: ForgotIn, session: AsyncSession = Depends(get_session)) -> dict:
    # Всегда 200 — не раскрываем, какие email зарегистрированы.
    acc = await account_service.get_by_email(session, body.email)
    if acc is not None and acc.email and acc.password_hash:
        await mail_service.send_password_reset(session, acc)
        await session.commit()
    return {"ok": True}


@router.post("/reset")
async def reset_password(body: ResetIn, session: AsyncSession = Depends(get_session)) -> dict:
    acc_id = await token_service.consume(session, body.token, token_service.RESET)
    if acc_id is None:
        await session.rollback()
        raise HTTPException(status_code=400, detail="Ссылка недействительна или устарела")
    acc = await account_service.get_by_id(session, acc_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    try:
        await account_service.set_password(session, acc, body.password)
    except account_service.AccountError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    await session.commit()
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.post("/login")
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        acc = await account_service.authenticate(session, body.email, body.password)
    except account_service.InvalidCredentials as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.post("/telegram")
async def telegram_auth(
    body: dict,
    authorization: Optional[str] = Header(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Вход через Telegram Login Widget или привязка к текущему аккаунту."""
    if not verify_login_widget(body, settings.telegram_bot_token):
        raise HTTPException(status_code=401, detail="Проверка Telegram не пройдена")
    tg_id = body.get("id")
    if not tg_id:
        raise HTTPException(status_code=400, detail="Нет id пользователя Telegram")

    current: Optional[WebAccount] = None
    if authorization and authorization.lower().startswith("bearer "):
        aid = decode_token(authorization.split(" ", 1)[1])
        if aid is not None:
            current = await account_service.get_by_id(session, aid)

    try:
        acc = await linking_service.login_or_link_telegram(session, int(tg_id), current)
    except linking_service.LinkError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"token": create_token(acc.id), "account": _dto(acc)}


@router.get("/me")
async def me(
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    bal = await wallet_service.get_balance(session, acc)
    return {**_dto(acc), "balance_rub": float(bal)}

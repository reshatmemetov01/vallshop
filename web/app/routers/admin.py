"""Админ-панель управления (admin.vallshop.com): отдельный вход, только владелец.

Вход — логин/пароль из настроек сервера (ADMIN_PANEL_USER/PASSWORD), не связан
с аккаунтами сайта. Токен — JWT с признаком adm. Эндпоинты защищены require_admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..deps import require_admin
from ..security import create_admin_token, verify_admin_credentials
from ..services import adminstats

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn) -> dict:
    if not verify_admin_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return {"token": create_admin_token()}


@router.get("/overview")
async def overview(
    preset: str = "7d",
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if preset not in adminstats.PRESETS:
        preset = "7d"
    total = await adminstats.total_block(session, preset)
    bot = await adminstats.bot_block(session, preset)
    site = await adminstats.site_block(session, preset)
    visits = await adminstats.visits_summary(session)
    return {"preset": preset, "total": total, "bot": bot, "site": site, "visits": visits}


@router.get("/visits")
async def visits(
    days: int = 30,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    days = max(1, min(int(days or 30), 180))
    return {
        "summary": await adminstats.visits_summary(session),
        "daily": await adminstats.visits_daily(session, days),
    }


@router.get("/visits/recent")
async def visits_recent(
    limit: int = 100,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"items": await adminstats.recent_visits(session, int(limit or 100))}


@router.post("/visits/reset")
async def visits_reset(
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    deleted = await adminstats.reset_visits(session)
    return {"ok": True, "deleted": deleted}


@router.get("/users")
async def users(
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await adminstats.site_users(session, q, int(limit or 50), int(offset or 0))

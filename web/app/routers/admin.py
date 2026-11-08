"""Админ-панель управления (admin.vallshop.com): отдельный вход, только владелец.

Вход — логин/пароль из настроек сервера (ADMIN_PANEL_USER/PASSWORD), не связан
с аккаунтами сайта. Токен — JWT с признаком adm. Эндпоинты защищены require_admin.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_session
from ..deps import require_admin
from ..security import create_admin_token, verify_admin_credentials
from ..services import adminstats

log = logging.getLogger("vallshop.web.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginIn(BaseModel):
    username: str
    password: str


class OrderActionIn(BaseModel):
    action: str  # deliver | refund | close_silent


async def _notify_customer(order, text: str) -> bool:
    """Отправить покупателю сообщение в Telegram (если привязан). Best-effort."""
    import httpx
    from bot.db.models import WebAccount
    from sqlalchemy import select
    from ..db import database

    token = settings.telegram_bot_token
    if not token:
        return False
    async with database.session() as s:
        acc = await s.scalar(select(WebAccount).where(WebAccount.wallet_user_id == order.user_id))
    chat_id = (acc.telegram_user_id if acc else None) or (order.user_id if order.source == "bot" else None)
    if not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            return r.status_code == 200
    except Exception:  # noqa: BLE001 — уведомление не должно ломать действие
        log.warning("Не удалось уведомить покупателя по заказу #%s", order.id)
        return False


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


@router.get("/orders")
async def orders(
    q: str = "",
    status: str = "",
    source: str = "",
    limit: int = 50,
    offset: int = 0,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await adminstats.orders_list(
        session, q, status, source, int(limit or 50), int(offset or 0)
    )


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: int,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    data = await adminstats.order_detail(session, order_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return data


@router.post("/orders/{order_id}/action")
async def order_action(
    order_id: int,
    body: OrderActionIn,
    _: bool = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ручное управление заказом из админки: выдать / вернуть / тихо закрыть."""
    from bot.db.models import Order, OrderStatus
    from bot.services import orders as order_service

    o = await session.get(Order, order_id)
    if o is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    action = (body.action or "").strip()
    out: dict = {"ok": True}
    notify_text = None

    if action == "refund":
        if o.status == OrderStatus.REFUNDED:
            raise HTTPException(status_code=400, detail="Заказ уже возвращён")
        refund = await order_service.cancel_order_manual(session, o)
        await session.commit()
        out["status"] = "refunded"
        out["refund_rub"] = float(refund)
        notify_text = f"↩️ Ваш заказ #{o.id} отменён. На баланс возвращено {refund:.0f} ₽."
    elif action in ("deliver", "close_silent"):
        if o.status == OrderStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Заказ уже выполнен")
        await order_service.deliver_topup_manual(session, o)
        await session.commit()
        out["status"] = "completed"
        if action == "deliver":
            notify_text = f"✅ Ваш заказ #{o.id} выполнен."
    else:
        raise HTTPException(status_code=400, detail="Неизвестное действие")

    out["notified"] = await _notify_customer(o, notify_text) if notify_text else False
    return out

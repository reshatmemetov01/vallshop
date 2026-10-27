"""История заказов аккаунта на сайте."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..db import get_session
from ..deps import current_account
from ..services import orders_view as orders_view_service

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("")
async def list_orders(
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"orders": await orders_view_service.list_orders(session, acc)}


@router.get("/{order_id}")
async def order_detail(
    order_id: int,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    dto = await orders_view_service.order_detail(session, acc, order_id)
    if dto is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return dto

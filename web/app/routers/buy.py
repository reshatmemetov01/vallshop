"""Покупка товаров с баланса на сайте."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..db import get_session
from ..deps import current_account
from ..services import checkout as checkout_service

router = APIRouter(prefix="/api/buy", tags=["buy"])


class BuyIn(BaseModel):
    variant_id: int
    quantity: int = 1
    fields: dict = {}          # доп. поля (напр. telegram_username для Звёзд/Премиум)


@router.post("")
async def buy(
    body: BuyIn,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        return await checkout_service.purchase(
            session, acc, body.variant_id, body.quantity, body.fields
        )
    except checkout_service.CheckoutError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stars-quote")
async def stars_quote(
    quantity: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Границы и цена для Telegram Звёзд (цена по живой котировке FazerCard)."""
    return await checkout_service.stars_quote(session, quantity)

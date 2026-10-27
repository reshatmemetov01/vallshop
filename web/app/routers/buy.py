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


@router.post("")
async def buy(
    body: BuyIn,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        return await checkout_service.purchase(
            session, acc, body.variant_id, body.quantity
        )
    except checkout_service.CheckoutError as e:
        raise HTTPException(status_code=400, detail=str(e))

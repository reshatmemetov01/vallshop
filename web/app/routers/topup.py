"""Пополнение баланса на сайте: способы, создание счёта, статус."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TopUpStatus, WebAccount

from ..db import get_session
from ..deps import current_account
from ..services import payments as payments_service
from ..services import wallet as wallet_service

router = APIRouter(prefix="/api/topup", tags=["topup"])


class TopupIn(BaseModel):
    amount: str          # сумма в рублях (строкой, чтобы не терять копейки)
    method: str          # card | sbp | sbp_ph


_TOPUP_STATUS_RU = {
    "pending": "ожидает оплату",
    "paid": "зачислено",
    "expired": "просрочено",
    "failed": "ошибка",
}
_PROVIDER_RU = {"payhot": "Карта", "paydex": "СБП", "boltutil": "USDT", "bolt": "USDT", "mock": "тест"}


@router.get("/history")
async def history(
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """История пополнений аккаунта."""
    from bot.db.models import HEAD_MERCHANT_ID
    from bot.services import balance as balance_service

    if acc.wallet_user_id is None:
        return {"topups": []}
    rows = await balance_service.list_topups(
        session, acc.wallet_user_id, HEAD_MERCHANT_ID, limit=30
    )
    out = []
    for t in rows:
        status = t.status.value if isinstance(t.status, TopUpStatus) else str(t.status)
        out.append({
            "id": t.id,
            "amount_rub": float(t.amount_usd or 0),
            "status": status,
            "status_label": _TOPUP_STATUS_RU.get(status, status),
            "method": _PROVIDER_RU.get(t.provider, t.provider or "—"),
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "paid": status == "paid",
        })
    return {"topups": out}


@router.get("/methods")
async def methods(session: AsyncSession = Depends(get_session)) -> dict:
    return {
        "methods": await payments_service.available_methods(session),
        "min": int(payments_service.TOPUP_MIN),
        "max": int(payments_service.TOPUP_MAX),
    }


@router.post("")
async def create(
    body: TopupIn,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        amount = Decimal(body.amount.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")
    try:
        topup = await payments_service.create_topup(session, acc, amount, body.method)
    except payments_service.TopupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await session.commit()
    return {
        "topup_id": topup.id,
        "checkout_url": topup.checkout_url,
        "status": "pending",
    }


@router.get("/{topup_id}")
async def status(
    topup_id: int,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    topup = await payments_service.get_topup(session, acc, topup_id)
    if topup is None:
        raise HTTPException(status_code=404, detail="Пополнение не найдено")
    balance = await wallet_service.get_balance(session, acc)
    return {
        "topup_id": topup.id,
        "status": str(topup.status.value if isinstance(topup.status, TopUpStatus) else topup.status),
        "amount_rub": float(topup.amount_usd),
        "balance_rub": float(balance),
    }

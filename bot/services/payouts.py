"""Выплаты заработка франшизам: кошельки, заявки на вывод, одобрение/отклонение.

Деньги владелец отправляет вручную — система хранит кошельки, заявки и баланс.
Суммы в рублях (для USDT-кошелька владелец конвертирует сам). Доступный баланс —
``Merchant.earnings_rub``; при создании заявки сумма резервируется (списывается),
при отклонении — возвращается.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, Merchant, MerchantPayout
from . import msettings as msettings_service

MIN_WITHDRAW = Decimal("500")

# Ключи кошельков в merchant_settings.
W_SBP_PHONE = "payout_sbp_phone"
W_SBP_BANK = "payout_sbp_bank"
W_USDT = "payout_usdt"

METHOD_NAMES = {"sbp": "СБП", "usdt": "USDT (BEP20)"}


# ── Кошельки ──────────────────────────────────────────────────────────────────
async def wallets(session: AsyncSession, merchant_id: int) -> dict:
    v = await msettings_service.all_for(session, merchant_id)
    sbp = None
    if v.get(W_SBP_PHONE):
        sbp = {"phone": v[W_SBP_PHONE], "bank": v.get(W_SBP_BANK, "")}
    return {"sbp": sbp, "usdt": v.get(W_USDT)}


async def set_sbp(session: AsyncSession, merchant_id: int, phone: str, bank: str) -> None:
    await msettings_service.set(session, merchant_id, W_SBP_PHONE, phone)
    await msettings_service.set(session, merchant_id, W_SBP_BANK, bank)


async def set_usdt(session: AsyncSession, merchant_id: int, address: str) -> None:
    await msettings_service.set(session, merchant_id, W_USDT, address)


def wallet_label(method: str, w: dict) -> str:
    """Человекочитаемые реквизиты кошелька для заявки/отображения."""
    if method == "sbp" and w.get("sbp"):
        s = w["sbp"]
        return f"СБП {s['phone']}" + (f" ({s['bank']})" if s.get("bank") else "")
    if method == "usdt" and w.get("usdt"):
        return f"USDT BEP20 {w['usdt']}"
    return "—"


# ── Баланс ────────────────────────────────────────────────────────────────────
async def available(session: AsyncSession, merchant_id: int) -> Decimal:
    m = await session.get(Merchant, merchant_id)
    return Decimal(m.earnings_rub or 0) if m else Decimal(0)


# ── Заявки ────────────────────────────────────────────────────────────────────
async def create_request(
    session: AsyncSession, merchant_id: int, amount: Decimal, method: str
) -> Tuple[Optional[MerchantPayout], Optional[str]]:
    """Создать заявку на вывод (резерв суммы). Возвращает (payout, ошибка)."""
    if merchant_id == HEAD_MERCHANT_ID:
        return None, "Недоступно"
    amount = Decimal(amount)
    if amount < MIN_WITHDRAW:
        return None, f"Минимальная сумма вывода — {int(MIN_WITHDRAW)} ₽."
    w = await wallets(session, merchant_id)
    if method == "sbp" and not w["sbp"]:
        return None, "Сначала привяжите кошелёк СБП."
    if method == "usdt" and not w["usdt"]:
        return None, "Сначала привяжите кошелёк USDT."
    m = await session.get(Merchant, merchant_id)
    bal = Decimal(m.earnings_rub or 0) if m else Decimal(0)
    if amount > bal:
        return None, f"Недостаточно средств. Доступно: {bal} ₽."
    m.earnings_rub = bal - amount  # резерв
    payout = MerchantPayout(
        merchant_id=merchant_id, amount_rub=amount, method=method,
        wallet=wallet_label(method, w), status="pending",
    )
    session.add(payout)
    await session.flush()
    return payout, None


async def get(session: AsyncSession, payout_id: int) -> Optional[MerchantPayout]:
    return await session.get(MerchantPayout, payout_id)


async def list_requests(
    session: AsyncSession, merchant_id: int, status: Optional[str] = None, limit: int = 20
) -> List[MerchantPayout]:
    stmt = select(MerchantPayout).where(MerchantPayout.merchant_id == merchant_id)
    if status is not None:
        stmt = stmt.where(MerchantPayout.status == status)
    stmt = stmt.order_by(MerchantPayout.id.desc()).limit(limit)
    return list(await session.scalars(stmt))


async def approve(session: AsyncSession, payout_id: int) -> Optional[MerchantPayout]:
    p = await session.get(MerchantPayout, payout_id)
    if p is None or p.status != "pending":
        return None
    p.status = "approved"
    p.decided_at = datetime.utcnow()
    await session.flush()
    return p


async def reject(session: AsyncSession, payout_id: int, reason: str) -> Optional[MerchantPayout]:
    p = await session.get(MerchantPayout, payout_id)
    if p is None or p.status != "pending":
        return None
    # Возврат зарезервированной суммы на баланс франшизы.
    m = await session.get(Merchant, p.merchant_id)
    if m is not None:
        m.earnings_rub = Decimal(m.earnings_rub or 0) + Decimal(p.amount_rub)
    p.status = "rejected"
    p.reason = (reason or "")[:255]
    p.decided_at = datetime.utcnow()
    await session.flush()
    return p

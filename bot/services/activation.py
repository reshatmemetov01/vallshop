"""Логика активации кодов: поиск кода, заявки, решения админа."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ActivationCode, ActivationRequest


async def find_code(session: AsyncSession, code: str) -> Optional[ActivationCode]:
    return await session.scalar(
        select(ActivationCode).where(ActivationCode.code == code)
    )


async def mark_used(session: AsyncSession, code: str, user_id: int) -> None:
    c = await find_code(session, code)
    if c:
        c.status = "used"
        c.used_by = user_id
        c.used_at = datetime.utcnow()
        await session.flush()


async def set_free(session: AsyncSession, code: str) -> None:
    c = await find_code(session, code)
    if c:
        c.status = "free"
        c.used_by = None
        c.used_at = None
        await session.flush()


async def create_request(
    session: AsyncSession,
    *,
    code: str,
    product: Optional[str],
    user_id: int,
    username: Optional[str],
    nickname: Optional[str],
    expected_price: Optional[int],
    actual_price: Optional[int],
    web_account_id: Optional[int] = None,
) -> ActivationRequest:
    req = ActivationRequest(
        code=code, product=product, user_id=user_id, username=username,
        nickname=nickname, expected_price=expected_price, actual_price=actual_price,
        status="review", web_account_id=web_account_id,
    )
    session.add(req)
    await session.flush()
    return req


async def list_for_account(session: AsyncSession, web_account_id: int):
    """Заявки конкретного веб-аккаунта (раздел «Мои активации»), новые сверху."""
    return list(await session.scalars(
        select(ActivationRequest)
        .where(ActivationRequest.web_account_id == web_account_id)
        .order_by(ActivationRequest.id.desc())
    ))


async def get_request(session: AsyncSession, request_id: int) -> Optional[ActivationRequest]:
    return await session.get(ActivationRequest, request_id)


async def decide_request(
    session: AsyncSession, request_id: int, status: str
) -> Optional[ActivationRequest]:
    req = await session.get(ActivationRequest, request_id)
    if req:
        req.status = status
        req.decided_at = datetime.utcnow()
        await session.flush()
    return req


async def code_counts(session: AsyncSession) -> dict:
    total = await session.scalar(select(func.count()).select_from(ActivationCode)) or 0
    free = await session.scalar(
        select(func.count()).select_from(ActivationCode).where(ActivationCode.status == "free")
    ) or 0
    used = await session.scalar(
        select(func.count()).select_from(ActivationCode).where(ActivationCode.status == "used")
    ) or 0
    return {"total": int(total), "free": int(free), "used": int(used)}


async def add_code(
    session: AsyncSession,
    *,
    code: str,
    product: str,
    instruction: Optional[str],
    robux_amount: Optional[int],
    status: str = "free",
) -> bool:
    """Добавить код. Возвращает False, если такой код уже есть."""
    exists = await find_code(session, code)
    if exists:
        return False
    session.add(ActivationCode(
        code=code, product=product, instruction=instruction,
        robux_amount=robux_amount, status=status,
    ))
    await session.flush()
    return True

"""Отзывы покупателей: создание, модерация, публичный список."""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, Review

PAGE_SIZE = 5


async def create_review(
    session: AsyncSession,
    *,
    order_id: Optional[int],
    user_id: int,
    username: Optional[str],
    variant_id: Optional[int],
    product_title: Optional[str],
    amount_usd: Optional[Decimal],
    rating: int,
    text: str,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> Review:
    review = Review(
        order_id=order_id,
        merchant_id=merchant_id,
        user_id=user_id,
        username=username,
        variant_id=variant_id,
        product_title=product_title,
        amount_usd=amount_usd,
        rating=rating,
        text=text,
        status="new",
    )
    session.add(review)
    await session.flush()
    return review


async def get(session: AsyncSession, review_id: int) -> Optional[Review]:
    return await session.get(Review, review_id)


async def update_text(session: AsyncSession, review_id: int, text: str) -> None:
    r = await session.get(Review, review_id)
    if r is not None:
        r.text = text
        await session.flush()


async def set_status(session: AsyncSession, review_id: int, status: str) -> None:
    r = await session.get(Review, review_id)
    if r is not None:
        r.status = status
        await session.flush()


async def delete(session: AsyncSession, review_id: int) -> None:
    r = await session.get(Review, review_id)
    if r is not None:
        await session.delete(r)
        await session.flush()


async def list_all(session: AsyncSession, limit: int = 50) -> List[Review]:
    return list(
        await session.scalars(
            select(Review).order_by(Review.id.desc()).limit(limit)
        )
    )


async def count_published(
    session: AsyncSession, merchant_id: int = HEAD_MERCHANT_ID
) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Review).where(
                Review.status == "published", Review.merchant_id == merchant_id
            )
        )
        or 0
    )


async def list_published(
    session: AsyncSession, *, page: int = 0, page_size: int = PAGE_SIZE,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> List[Review]:
    return list(
        await session.scalars(
            select(Review)
            .where(Review.status == "published", Review.merchant_id == merchant_id)
            .order_by(Review.id.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
    )

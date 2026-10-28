"""Опубликованные отзывы головного магазина — для показа на сайте."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import HEAD_MERCHANT_ID
from bot.services import reviews as review_service

from ..db import get_session

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.get("")
async def list_reviews(
    limit: int = 12, session: AsyncSession = Depends(get_session)
) -> dict:
    try:
        items = await review_service.list_published(
            session, page=0, page_size=max(1, min(limit, 30)),
            merchant_id=HEAD_MERCHANT_ID,
        )
    except Exception:  # noqa: BLE001 — отзывы не должны ронять страницу
        return {"reviews": []}
    return {
        "reviews": [
            {
                "product_title": r.product_title,
                "rating": int(r.rating or 5),
                "text": r.text,
                "username": r.username,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in items
        ]
    }

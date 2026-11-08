"""Описания товаров/разделов для сайта (site-only, отдельно от бота)."""

from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SiteProductDesc


async def get_one(session: AsyncSession, product_id: int) -> Optional[str]:
    row = await session.get(SiteProductDesc, product_id)
    return row.description if row and row.description else None


async def get_many(session: AsyncSession, product_ids: List[int]) -> Dict[int, str]:
    if not product_ids:
        return {}
    rows = await session.scalars(
        select(SiteProductDesc).where(SiteProductDesc.product_id.in_(product_ids))
    )
    return {r.product_id: r.description for r in rows if r.description}


async def sections_tree(session: AsyncSession) -> List[dict]:
    """Плоский список всех товаров/категорий каталога (с отступом по глубине)
    и их site-описанием — для редактора «Оформление» в админке."""
    from bot.services import catalog as catalog_service

    out: List[dict] = []

    async def walk(parent_id, depth, path):
        products = await catalog_service.list_products(
            session, parent_id=parent_id, exclude_head_only=False
        )
        for p in products:
            out.append({"id": p.id, "title": p.title, "depth": depth, "path": path})
            await walk(p.id, depth + 1, (path + " › " if path else "") + (p.title or ""))

    await walk(None, 0, "")
    descs = await get_many(session, [s["id"] for s in out])
    for s in out:
        s["description"] = descs.get(s["id"], "")
        s["has_desc"] = bool(descs.get(s["id"]))
    return out


async def set_one(session: AsyncSession, product_id: int, text: Optional[str]) -> None:
    text = (text or "").strip()
    row = await session.get(SiteProductDesc, product_id)
    if not text:
        if row is not None:
            await session.execute(
                delete(SiteProductDesc).where(SiteProductDesc.product_id == product_id)
            )
        await session.commit()
        return
    if row is None:
        session.add(SiteProductDesc(product_id=product_id, description=text))
    else:
        row.description = text
    await session.commit()

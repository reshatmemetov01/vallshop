"""Каталог для сайта (чтение). Переиспользует бизнес-логику бота.

Цены считаются ТАК ЖЕ, как в боте (merchant_id=0 — головной магазин):
owner-цена через ``pricing.owner_price_rub`` → клиентская через ``mprice``.
Наличие buy-on-demand источников (FazerCard/soc-proof/VPN) на этом этапе —
«под заказ»; живую проверку у поставщика подключим в Phase 2.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import Product, Variant
from bot.services import catalog as catalog_service
from bot.services import mprice as mprice_service
from bot.services import stock as stock_service
from bot.services.pricing import owner_price_rub

from ..config import settings
from ..db import get_session
from ..services.checkout import buy_kind, web_buyable

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

_ONDEMAND = {"fazercard", "socproof", "vpnresellers"}


def _product_dto(p) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "icon_emoji_id": p.icon_emoji_id,
        "banner_file_id": p.banner_file_id,
        "button_style": p.button_style,
        "full_width": bool(p.full_width),
    }


@router.get("")
async def list_catalog(parent_id: int | None = None,
                       session: AsyncSession = Depends(get_session)) -> dict:
    """Товары уровня ``parent_id`` (по умолчанию — верхний уровень)."""
    products = await catalog_service.list_products(
        session, parent_id=parent_id, exclude_head_only=False
    )
    out = []
    for p in products:
        dto = _product_dto(p)
        dto["has_children"] = await catalog_service.children_count(session, p.id) > 0
        out.append(dto)
    return {"parent_id": parent_id, "products": out}


@router.get("/{product_id}")
async def product_detail(product_id: int,
                         session: AsyncSession = Depends(get_session)) -> dict:
    """Либо подкатегории (если товар-категория), либо номиналы с ценами."""
    product = await catalog_service.get_product(session, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    children = await catalog_service.list_children(session, product_id)
    if children:
        return {
            "type": "category",
            "product": _product_dto(product),
            "children": [
                {**_product_dto(c),
                 "has_children": await catalog_service.children_count(session, c.id) > 0}
                for c in children
            ],
        }

    variants = await catalog_service.list_variants(session, product_id)
    items = await _variant_items(session, variants)
    return {"type": "product", "product": _product_dto(product), "variants": items}


async def _variant_items(session, variants, *, with_product: bool = False) -> list:
    """Собрать DTO номиналов: цены, наличие, доступность покупки на сайте."""
    owner_prices = {v.id: owner_price_rub(v, settings.default_markup_percent) for v in variants}
    client = await mprice_service.client_prices(session, settings.merchant_id, owner_prices)
    counts = await stock_service.counts_by_variant(session, [v.id for v in variants])
    items = []
    for v in variants:
        src = v.source or "stock"
        if src in _ONDEMAND:
            available = True
            stock = None
        else:
            stock = int(counts.get(v.id, 0) or 0)
            available = stock > 0
        price = client.get(v.id)
        dto = {
            "id": v.id,
            "title": v.title,
            "source": src,
            "price_rub": float(price) if price and price > 0 else None,
            "per_1000": src == "socproof",
            "smm_min": v.smm_min,
            "smm_max": v.smm_max,
            "available": available,
            "stock": stock,
            "description": v.description,
            "buyable": web_buyable(v),
            "buy_kind": buy_kind(v),
        }
        if with_product and v.product is not None:
            dto["product_title"] = v.product.title
            dto["product_id"] = v.product_id
        items.append(dto)
    return items


@router.get("/search/q")
async def search(q: str = "", session: AsyncSession = Depends(get_session)) -> dict:
    """Поиск номиналов по названию (товара и номинала). Возвращает купибельные."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "variants": []}
    like = f"%{q}%"
    rows = await session.scalars(
        select(Variant)
        .join(Product, Variant.product_id == Product.id)
        .where(
            Variant.is_active.is_(True),
            Product.is_active.is_(True),
            or_(Variant.title.ilike(like), Product.title.ilike(like)),
        )
        .options(selectinload(Variant.product))
        .limit(40)
    )
    variants = list(rows)
    items = await _variant_items(session, variants, with_product=True)
    return {"query": q, "variants": items}

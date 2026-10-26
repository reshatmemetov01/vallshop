"""Каталог для сайта (чтение). Переиспользует бизнес-логику бота.

Цены считаются ТАК ЖЕ, как в боте (merchant_id=0 — головной магазин):
owner-цена через ``pricing.owner_price_rub`` → клиентская через ``mprice``.
Наличие buy-on-demand источников (FazerCard/soc-proof/VPN) на этом этапе —
«под заказ»; живую проверку у поставщика подключим в Phase 2.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services import catalog as catalog_service
from bot.services import mprice as mprice_service
from bot.services import stock as stock_service
from bot.services.pricing import owner_price_rub

from ..config import settings
from ..db import get_session

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
    owner_prices = {v.id: owner_price_rub(v, settings.default_markup_percent) for v in variants}
    client = await mprice_service.client_prices(session, settings.merchant_id, owner_prices)
    counts = await stock_service.counts_by_variant(session, [v.id for v in variants])

    items = []
    for v in variants:
        src = v.source or "stock"
        if src in _ONDEMAND:
            available = True          # Phase 2: живая проверка у поставщика
            stock = None
        else:
            stock = int(counts.get(v.id, 0) or 0)
            available = stock > 0
        price = client.get(v.id)
        items.append({
            "id": v.id,
            "title": v.title,
            "source": src,
            "price_rub": float(price) if price and price > 0 else None,
            "per_1000": src == "socproof",   # SMM — цена за 1000
            "smm_min": v.smm_min,
            "smm_max": v.smm_max,
            "available": available,
            "stock": stock,
            "description": v.description,
        })
    return {"type": "product", "product": _product_dto(product), "variants": items}

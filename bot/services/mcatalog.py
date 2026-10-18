"""Пер-мерчантное отображение каталога: подпись/эмодзи/описание товара и номинала.

Каталог общий (товары/номиналы владельца). Франшиза может переопределить, как
они показаны в ЕЁ боте, не меняя общий каталог. Головной (merchant_id = 0)
переопределений не имеет — показываются поля самого товара/номинала.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, MerchantCatalogOverride

PRODUCT = "product"
VARIANT = "variant"


async def get_override(
    session: AsyncSession, merchant_id: int, item_type: str, item_id: int
) -> Optional[MerchantCatalogOverride]:
    if merchant_id == HEAD_MERCHANT_ID:
        return None
    return await session.get(
        MerchantCatalogOverride,
        {"merchant_id": merchant_id, "item_type": item_type, "item_id": item_id},
    )


async def overrides_map(
    session: AsyncSession, merchant_id: int, item_type: str, ids: Iterable[int]
) -> Dict[int, dict]:
    """Пакетно: {item_id: {"title","emoji_id","description"}} для франшизы.

    Для головного — пустой словарь (переопределений нет).
    """
    ids = list(ids)
    if merchant_id == HEAD_MERCHANT_ID or not ids:
        return {}
    rows = await session.scalars(
        select(MerchantCatalogOverride).where(
            MerchantCatalogOverride.merchant_id == merchant_id,
            MerchantCatalogOverride.item_type == item_type,
            MerchantCatalogOverride.item_id.in_(ids),
        )
    )
    return {
        r.item_id: {"title": r.title, "emoji_id": r.emoji_id, "description": r.description}
        for r in rows
    }


# ── Резолверы отображения (ov = запись переопределения или dict или None) ──────
def _ov_get(ov, field: str):
    if ov is None:
        return None
    if isinstance(ov, dict):
        return ov.get(field)
    return getattr(ov, field, None)


def disp_title(obj, ov) -> str:
    t = _ov_get(ov, "title")
    return t if t else obj.title


def disp_emoji(obj, ov) -> Optional[str]:
    """Эмодзи: своё (""=без эмодзи) важнее дефолтного из каталога."""
    e = _ov_get(ov, "emoji_id")
    if e is None:
        return getattr(obj, "icon_emoji_id", None)
    return e or None  # "" → без эмодзи


def disp_description(obj, ov) -> Optional[str]:
    d = _ov_get(ov, "description")
    if d is None:
        return getattr(obj, "description", None)
    return d or None


# ── Админка франшизы (запись) ─────────────────────────────────────────────────
async def _get_or_create(session, merchant_id, item_type, item_id) -> MerchantCatalogOverride:
    ov = await get_override(session, merchant_id, item_type, item_id)
    if ov is None:
        ov = MerchantCatalogOverride(
            merchant_id=merchant_id, item_type=item_type, item_id=item_id
        )
        session.add(ov)
        await session.flush()
    return ov


async def set_title(session, merchant_id, item_type, item_id, title: Optional[str]) -> None:
    ov = await _get_or_create(session, merchant_id, item_type, item_id)
    ov.title = title  # None = сброс к дефолту
    await session.flush()


async def set_emoji(session, merchant_id, item_type, item_id, emoji_id: Optional[str]) -> None:
    ov = await _get_or_create(session, merchant_id, item_type, item_id)
    ov.emoji_id = emoji_id  # None = дефолт, "" = без эмодзи
    await session.flush()


async def set_description(session, merchant_id, item_type, item_id, text: Optional[str]) -> None:
    ov = await _get_or_create(session, merchant_id, item_type, item_id)
    ov.description = text
    await session.flush()


async def clear(session, merchant_id, item_type, item_id) -> None:
    ov = await get_override(session, merchant_id, item_type, item_id)
    if ov is not None:
        await session.delete(ov)
        await session.flush()

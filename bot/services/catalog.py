"""Запросы каталога: активные товары и их вариации."""

from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Dict, List, Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import (
    Order,
    Payment,
    Product,
    ReferralEarning,
    Review,
    StockItem,
    Variant,
    VpnSubscription,
)

log = logging.getLogger("vallshop.catalog")


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def fazercard_stock(fzr, variants: List[Variant]) -> Dict[int, Optional[int]]:
    """Живой сток FazerCard для номиналов source='fazercard'.

    Возвращает ``{variant_id: stock}``; ``None`` — если сток неизвестен
    (ошибка запроса / поле отсутствует). Группирует по категории, чтобы
    сделать один запрос на (kind, category_id). Для не-FazerCard номиналов
    ключей не добавляет.
    """
    result: Dict[int, Optional[int]] = {}
    groups: Dict[tuple, List[Variant]] = {}
    for v in variants:
        if getattr(v, "source", "stock") == "fazercard" and v.fzr_a and v.fzr_b:
            groups.setdefault((v.fzr_kind, v.fzr_a), []).append(v)
    if not groups or fzr is None:
        return result

    for (kind, cat_id), vs in groups.items():
        try:
            offers = await asyncio.to_thread(fzr.offers_for, kind, cat_id)
            smap = {str(o.get("id")): _as_int(o.get("stock")) for o in offers}
        except Exception:  # noqa: BLE001 — витрина не должна падать из-за поставщика
            log.warning("Не удалось получить сток FazerCard для %s/%s", kind, cat_id)
            smap = {}
            for v in vs:
                result[v.id] = None
            continue
        for v in vs:
            key = str(v.fzr_b)
            # Оффер присутствует у поставщика → его сток (int, либо None если поле
            # не пришло — товар под заказ). Оффер ОТСУТСТВУЕТ в выдаче поставщика →
            # 0 (нет в наличии): иначе показывали бы «в наличии» и покупка падала.
            result[v.id] = smap[key] if key in smap else 0
    return result


async def fazercard_cost(fzr, variants: List[Variant]) -> Dict[int, "object"]:
    """Живой закуп (USD, Decimal) для номиналов source='fazercard'.

    Возвращает ``{variant_id: Decimal}``; если закуп неизвестен — ключа нет.
    Группирует по (kind, category_id) — один запрос на категорию.
    """
    from decimal import Decimal as _D
    result: Dict[int, object] = {}
    groups: Dict[tuple, List[Variant]] = {}
    for v in variants:
        if getattr(v, "source", "stock") == "fazercard" and v.fzr_a and v.fzr_b:
            groups.setdefault((v.fzr_kind, v.fzr_a), []).append(v)
    if not groups or fzr is None:
        return result
    for (kind, cat_id), vs in groups.items():
        try:
            offers = await asyncio.to_thread(fzr.offers_for, kind, cat_id)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось получить закуп FazerCard для %s/%s", kind, cat_id)
            continue
        cmap: Dict[str, object] = {}
        for o in offers:
            pu = o.get("price_usd")
            if pu is None:
                continue
            try:
                cmap[str(o.get("id"))] = _D(str(pu))
            except Exception:  # noqa: BLE001
                pass
        for v in vs:
            c = cmap.get(str(v.fzr_b))
            if c is not None:
                result[v.id] = c
    return result


async def list_products(
    session: AsyncSession,
    *,
    only_active: bool = True,
    parent_id: Optional[int] = None,
    exclude_head_only: bool = False,
) -> List[Product]:
    """Товары уровня ``parent_id`` (по умолчанию — верхний уровень, parent IS NULL).

    ``exclude_head_only=True`` (каталог франшизы) скрывает товары, помеченные
    «только для головного» (Product.head_only).
    """
    stmt = select(Product).order_by(Product.sort_order, Product.id)
    if parent_id is None:
        stmt = stmt.where(Product.parent_id.is_(None))
    else:
        stmt = stmt.where(Product.parent_id == parent_id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    if exclude_head_only:
        stmt = stmt.where(Product.head_only.is_(False))
    return list(await session.scalars(stmt))


async def list_children(
    session: AsyncSession, parent_id: int, *, only_active: bool = True,
    exclude_head_only: bool = False,
) -> List[Product]:
    return await list_products(
        session, only_active=only_active, parent_id=parent_id,
        exclude_head_only=exclude_head_only,
    )


async def children_count(session: AsyncSession, parent_id: int) -> int:
    from sqlalchemy import func
    return int(
        await session.scalar(
            select(func.count()).select_from(Product).where(Product.parent_id == parent_id)
        )
        or 0
    )


async def get_product(
    session: AsyncSession, product_id: int, *, with_variants: bool = True
) -> Optional[Product]:
    stmt = select(Product).where(Product.id == product_id)
    if with_variants:
        stmt = stmt.options(selectinload(Product.variants))
    return await session.scalar(stmt)


async def list_variants(
    session: AsyncSession, product_id: int, *, only_active: bool = True
) -> List[Variant]:
    stmt = (
        select(Variant)
        .where(Variant.product_id == product_id)
        .order_by(Variant.sort_order, Variant.id)
    )
    if only_active:
        stmt = stmt.where(Variant.is_active.is_(True))
    return list(await session.scalars(stmt))


async def bulk_create_fzr_variants(
    session: AsyncSession,
    product_id: int,
    kind: str,
    cat: dict,
    offers: list,
    markup_pct: Decimal,
) -> tuple[int, int]:
    """Пакетно создать номиналы FazerCard из ``offers`` (в порядке списка).

    Цена каждого = закуп × (1 + markup_pct/100), хранится в USD (плавает по
    курсу). Номиналы, уже добавленные к товару (по ``fzr_b``), пропускаются.
    ``sort_order`` назначается по порядку списка — после существующих номиналов.

    Возвращает ``(добавлено, пропущено)``.
    """
    existing = set(
        await session.scalars(
            select(Variant.fzr_b).where(
                Variant.product_id == product_id, Variant.fzr_b.is_not(None)
            )
        )
    )
    min_vid = await session.scalar(
        select(func.min(Variant.liog_variation_id)).where(
            Variant.product_id == product_id
        )
    )
    next_vid = min(0, int(min_vid or 0))
    max_sort = await session.scalar(
        select(func.max(Variant.sort_order)).where(Variant.product_id == product_id)
    )
    base_sort = int(max_sort or 0)

    factor = Decimal(1) + Decimal(markup_pct) / Decimal(100)
    added = skipped = 0
    for o in offers:
        offer_id = str(o.get("id"))
        if offer_id in existing:
            skipped += 1
            continue
        try:
            cost = Decimal(str(o.get("price_usd") or "0"))
        except (InvalidOperation, TypeError):
            cost = Decimal("0")
        price = (cost * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        next_vid -= 1
        base_sort += 1
        title = f"{cat.get('name')} · {o.get('name')}".strip(" ·")
        session.add(
            Variant(
                product_id=product_id, title=title[:255],
                liog_product_id=0, liog_variation_id=next_vid,
                cost_usd=cost, price_usd=price,
                source="fazercard", fzr_kind=kind,
                fzr_a=str(cat.get("id")), fzr_b=offer_id,
                sort_order=base_sort,
            )
        )
        existing.add(offer_id)
        added += 1
    if added:
        await session.commit()
    return added, skipped


async def bulk_create_smm_variants(
    session: AsyncSession,
    product_id: int,
    services: list,
    markup_pct: Decimal,
) -> tuple[int, int]:
    """Пакетно создать SMM-услуги (source="socproof") из ``services``.

    Каждая услуга — нормализованный dict soc-proof
    ``{id, name, rate, min, max, ...}``. Наценка (``markup_pct``) — общая для всех
    и хранится в ``markup_percent``; закуп ₽/1000 — в ``smm_rate``. Услуги, уже
    добавленные к товару (по ``smm_service``), пропускаются. ``sort_order`` —
    по порядку списка (после существующих номиналов).

    Возвращает ``(добавлено, пропущено)``.
    """
    existing = {
        int(x)
        for x in await session.scalars(
            select(Variant.smm_service).where(
                Variant.product_id == product_id, Variant.smm_service.is_not(None)
            )
        )
    }
    min_vid = await session.scalar(
        select(func.min(Variant.liog_variation_id)).where(
            Variant.product_id == product_id
        )
    )
    next_vid = min(0, int(min_vid or 0))
    max_sort = await session.scalar(
        select(func.max(Variant.sort_order)).where(Variant.product_id == product_id)
    )
    base_sort = int(max_sort or 0)

    markup = Decimal(markup_pct)
    added = skipped = 0
    for s in services:
        sid = int(s.get("id") or 0)
        if not sid or sid in existing:
            skipped += 1
            continue
        try:
            rate = Decimal(str(s.get("rate") or "0"))
        except (InvalidOperation, TypeError):
            rate = Decimal("0")
        next_vid -= 1
        base_sort += 1
        session.add(
            Variant(
                product_id=product_id, title=(s.get("name") or str(sid))[:255],
                liog_product_id=0, liog_variation_id=next_vid,
                cost_usd=Decimal("0"), price_usd=None,
                source="socproof", markup_percent=markup,
                smm_service=sid, smm_min=int(s.get("min") or 0),
                smm_max=int(s.get("max") or 0), smm_rate=rate,
                sort_order=base_sort,
            )
        )
        existing.add(sid)
        added += 1
    if added:
        await session.commit()
    return added, skipped


async def resolve_text(
    session: AsyncSession, variant: Variant, field: str, default: str
) -> str:
    """Текст с наследованием: номинал → товар → родительский товар → дефолт.

    Значение ``None`` на уровне = «наследовать дальше», ``""`` = «скрыть»
    (останавливает наследование и возвращает пустую строку).
    """
    v = getattr(variant, field, None)
    if v is not None:
        return v
    product = await session.get(Product, variant.product_id)
    if product is not None:
        pv = getattr(product, field, None)
        if pv is not None:
            return pv
        if product.parent_id:
            parent = await session.get(Product, product.parent_id)
            if parent is not None:
                ppv = getattr(parent, field, None)
                if ppv is not None:
                    return ppv
    return default


async def resolve_text_for_product(
    session: AsyncSession, product: Product, field: str, default: str
) -> str:
    """То же наследование, но начиная с товара: товар → родитель → дефолт.

    Используется в админке, чтобы показать действующий текст на уровне
    категории/подкатегории (без конкретного номинала).
    """
    pv = getattr(product, field, None)
    if pv is not None:
        return pv
    if product.parent_id:
        parent = await session.get(Product, product.parent_id)
        if parent is not None:
            ppv = getattr(parent, field, None)
            if ppv is not None:
                return ppv
    return default


async def _descendant_product_ids(session: AsyncSession, root_id: int) -> List[int]:
    """Сам товар + все вложенные подкатегории (на любую глубину)."""
    ids = [root_id]
    frontier = [root_id]
    while frontier:
        children = list(
            await session.scalars(
                select(Product.id).where(Product.parent_id.in_(frontier))
            )
        )
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


async def _variant_order_count(session: AsyncSession, variant_ids: List[int]) -> int:
    if not variant_ids:
        return 0
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.variant_id.in_(variant_ids))
        )
        or 0
    )


async def delete_variant(session: AsyncSession, variant_id: int) -> tuple[bool, str]:
    """Удалить номинал. Нельзя, если по нему есть заказы (ломает историю).

    Возвращает ``(ok, reason)``: reason ∈ {"ok", "orders", "missing"}.
    Сток-коды удаляются каскадно.
    """
    v = await session.get(Variant, variant_id)
    if v is None:
        return False, "missing"
    if await _variant_order_count(session, [variant_id]):
        return False, "orders"
    await session.execute(sa_delete(StockItem).where(StockItem.variant_id == variant_id))
    await session.execute(sa_delete(Variant).where(Variant.id == variant_id))
    await session.commit()
    return True, "ok"


async def variant_order_count(session: AsyncSession, variant_id: int) -> int:
    """Сколько заказов привязано к номиналу (для предупреждений при удалении)."""
    return await _variant_order_count(session, [variant_id])


async def product_order_count(session: AsyncSession, product_id: int) -> int:
    """Сколько заказов по всем номиналам товара/категории (с вложенностью)."""
    ids = await _descendant_product_ids(session, product_id)
    var_ids = list(
        await session.scalars(select(Variant.id).where(Variant.product_id.in_(ids)))
    )
    return await _variant_order_count(session, var_ids)


async def _purge_orders(session: AsyncSession, var_ids: List[int]) -> int:
    """Удалить заказы по номиналам и все зависящие от них записи.

    Чистит платежи, реферальные начисления, VPN-подписки и отзывы, завязанные
    на эти заказы, чтобы не осталось «висячих» ссылок. Возвращает число
    удалённых заказов. НЕ коммитит (делает вызывающий код).
    """
    if not var_ids:
        return 0
    order_ids = list(
        await session.scalars(select(Order.id).where(Order.variant_id.in_(var_ids)))
    )
    if order_ids:
        for model in (Payment, ReferralEarning, VpnSubscription, Review):
            await session.execute(sa_delete(model).where(model.order_id.in_(order_ids)))
        await session.execute(sa_delete(Order).where(Order.id.in_(order_ids)))
    return len(order_ids)


async def force_delete_variant(session: AsyncSession, variant_id: int) -> tuple[bool, str]:
    """Удалить номинал ВМЕСТЕ с заказами по нему (необратимо, влияет на статистику)."""
    v = await session.get(Variant, variant_id)
    if v is None:
        return False, "missing"
    await _purge_orders(session, [variant_id])
    await session.execute(sa_delete(StockItem).where(StockItem.variant_id == variant_id))
    await session.execute(sa_delete(Variant).where(Variant.id == variant_id))
    await session.commit()
    return True, "ok"


async def delete_product(session: AsyncSession, product_id: int) -> tuple[bool, str]:
    """Удалить товар/категорию со всеми подкатегориями и номиналами.

    Нельзя, если хоть по одному номиналу (на любом уровне вложенности) есть
    заказы. Возвращает ``(ok, reason)``: reason ∈ {"ok", "orders", "missing"}.
    """
    root = await session.get(Product, product_id)
    if root is None:
        return False, "missing"
    ids = await _descendant_product_ids(session, product_id)
    var_ids = list(
        await session.scalars(select(Variant.id).where(Variant.product_id.in_(ids)))
    )
    if await _variant_order_count(session, var_ids):
        return False, "orders"
    if var_ids:
        await session.execute(
            sa_delete(StockItem).where(StockItem.variant_id.in_(var_ids))
        )
        await session.execute(sa_delete(Variant).where(Variant.id.in_(var_ids)))
    await session.execute(sa_delete(Product).where(Product.id.in_(ids)))
    await session.commit()
    return True, "ok"


async def force_delete_product(session: AsyncSession, product_id: int) -> tuple[bool, str]:
    """Удалить товар/категорию ВМЕСТЕ с заказами по всем номиналам.

    Необратимо и задним числом меняет статистику (выручка/прибыль по удалённым
    заказам пропадают). Возвращает ``(ok, reason)``: reason ∈ {"ok", "missing"}.
    """
    root = await session.get(Product, product_id)
    if root is None:
        return False, "missing"
    ids = await _descendant_product_ids(session, product_id)
    var_ids = list(
        await session.scalars(select(Variant.id).where(Variant.product_id.in_(ids)))
    )
    await _purge_orders(session, var_ids)
    if var_ids:
        await session.execute(
            sa_delete(StockItem).where(StockItem.variant_id.in_(var_ids))
        )
        await session.execute(sa_delete(Variant).where(Variant.id.in_(var_ids)))
    await session.execute(sa_delete(Product).where(Product.id.in_(ids)))
    await session.commit()
    return True, "ok"


async def list_all_products(
    session: AsyncSession, *, only_active: bool = False
) -> List[Product]:
    """Все товары/категории (любого уровня), отсортированные для меню."""
    stmt = select(Product).order_by(
        Product.parent_id, Product.sort_order, Product.id
    )
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    return list(await session.scalars(stmt))


async def move_candidates(session: AsyncSession, product_id: int) -> List[Product]:
    """Куда можно переместить товар: все, кроме самого себя и его подкатегорий."""
    blocked = set(await _descendant_product_ids(session, product_id))
    return [p for p in await list_all_products(session) if p.id not in blocked]


async def reparent_product(
    session: AsyncSession, product_id: int, new_parent_id: Optional[int]
) -> tuple[bool, str]:
    """Переместить товар/категорию внутрь другой категории (или на верх).

    ``new_parent_id=None`` — вынести на верхний уровень. Нельзя переместить
    товар в себя или в свою же подкатегорию (цикл). Возвращает
    ``(ok, reason)``: reason ∈ {"ok", "self", "cycle", "missing"}.
    """
    p = await session.get(Product, product_id)
    if p is None:
        return False, "missing"
    if new_parent_id is not None:
        if new_parent_id == product_id:
            return False, "self"
        if new_parent_id in await _descendant_product_ids(session, product_id):
            return False, "cycle"
        if await session.get(Product, new_parent_id) is None:
            return False, "missing"
    p.parent_id = new_parent_id
    await session.commit()
    return True, "ok"


async def get_variant(session: AsyncSession, variant_id: int) -> Optional[Variant]:
    # Жадно подгружаем product, чтобы обращение к variant.product не вызывало
    # ленивую загрузку в async-контексте (SQLAlchemy async её не допускает).
    return await session.scalar(
        select(Variant)
        .where(Variant.id == variant_id)
        .options(selectinload(Variant.product))
    )

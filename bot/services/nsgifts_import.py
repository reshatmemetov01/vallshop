"""Импорт каталога NS.Gifts в скрытый раздел «NS.Gifts».

Этап 1: товары-коды (гифт-карты, ключи) — схема полей заказа ``quantity``
(покупаешь N штук, получаешь коды). Это основная масса каталога.

Структура:
    NS.Gifts                      (раздел верхнего уровня, СКРЫТ)
      └ Nintendo Gift Card | EU   (категория NS.Gifts, скрыта)
          └ 15 EUR                (номинал = service, скрыт)
          └ 25 EUR …

Variant: source="nsgifts", fzr_a=category_id, fzr_b=service_id, fzr_kind="code",
цена smart с наценкой 9%. Идемпотентно: номинал матчится по service_id (fzr_b),
категория — по названию внутри раздела. Всё создаётся СКРЫТЫМ — включаете нужное
в админке.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from ..db import Database
from ..db.models import Product, Variant
from . import rates as rates_service
from . import smartprice as smartprice_service

log = logging.getLogger("vallshop.nsgifts.import")

SECTION_TITLE = "NS.Gifts"
MARKUP = Decimal("9")              # наценка 9% (режим smart)


def _is_code_category(cat: dict) -> bool:
    """Категория-код: единственное поле заказа — quantity."""
    flds = cat.get("fields") or []
    return len(flds) == 1 and (flds[0].get("key") == "quantity")


def _variant_title(service_name: Optional[str], category_name: Optional[str]) -> str:
    """Короткое имя номинала: убираем префикс названия категории.

    «Nintendo Gift Card | EU | 15 EUR» под категорией «Nintendo Gift Card | EU»
    → «15 EUR». Иначе — последний сегмент после «|»."""
    s = (service_name or "").strip()
    c = (category_name or "").strip()
    if c and s.startswith(c):
        rest = s[len(c):].lstrip(" |").strip()
        if rest:
            return rest
    parts = [p.strip() for p in s.split("|") if p.strip()]
    return parts[-1] if parts else (s or "—")


async def _ensure_section(session) -> Product:
    sec = await session.scalar(
        select(Product).where(Product.title == SECTION_TITLE, Product.parent_id.is_(None)).limit(1)
    )
    if sec is not None:
        return sec
    max_sort = await session.scalar(
        select(func.max(Product.sort_order)).where(Product.parent_id.is_(None))
    )
    sec = Product(
        game=SECTION_TITLE[:64], title=SECTION_TITLE[:255], parent_id=None,
        sort_order=int(max_sort or 0) + 1, is_active=False,  # раздел скрыт
    )
    session.add(sec)
    await session.flush()
    return sec


async def _ensure_category(session, section: Product, name: str) -> tuple:
    name = (name or "—").strip()[:255]
    child = await session.scalar(
        select(Product).where(Product.parent_id == section.id, Product.title == name).limit(1)
    )
    if child is not None:
        return child, False
    max_sort = await session.scalar(
        select(func.max(Product.sort_order)).where(Product.parent_id == section.id)
    )
    child = Product(
        game=name[:64], title=name, parent_id=section.id,
        sort_order=int(max_sort or 0) + 1, is_active=False,  # категория скрыта
    )
    session.add(child)
    await session.flush()
    return child, True


async def run_import(db: Database, nsg, config=None) -> str:
    """Импорт/досоздание товаров-кодов NS.Gifts. Возвращает текст-отчёт."""
    if nsg is None or not getattr(nsg, "configured", False):
        return "NS.Gifts не настроен (NSGIFTS_* в .env) — импорт недоступен."
    try:
        stock = await asyncio.to_thread(nsg.stock)
    except Exception as e:  # noqa: BLE001
        log.exception("nsgifts: не удалось получить каталог")
        return f"Не удалось получить каталог NS.Gifts:\n<code>{type(e).__name__}: {e}</code>"

    cats = stock.get("categories") or []
    code_cats = [c for c in cats if _is_code_category(c)]
    if not code_cats:
        return "В каталоге NS.Gifts не найдено товаров-кодов (схема quantity)."

    rate = rates_service.get_rate()
    n_cat = n_newcat = n_var = n_skip = 0

    # Один раз берём уже импортированные service_id — чтобы не делать запрос на
    # каждый номинал (каталог большой).
    async with db.session() as session:
        await _ensure_section(session)
        existing_sids = set(
            str(x) for x in await session.scalars(
                select(Variant.fzr_b).where(Variant.source == "nsgifts")
            )
        )
        await session.commit()

    for c in code_cats:
        cat_name = c.get("category_name")
        cat_id = str(c.get("category_id"))
        services = c.get("services") or []
        if not services:
            continue
        async with db.session() as session:
            section = await _ensure_section(session)
            prod, created = await _ensure_category(session, section, cat_name)
            n_cat += 1
            if created:
                n_newcat += 1
            min_vid = await session.scalar(
                select(func.min(Variant.liog_variation_id)).where(Variant.product_id == prod.id)
            )
            next_lvid = min(0, int(min_vid or 0)) - 1
            for s in services:
                sid = s.get("service_id")
                if sid is None:
                    continue
                sid = str(sid)
                if sid in existing_sids:
                    n_skip += 1
                    continue
                try:
                    cost = Decimal(str(s.get("price")))
                except Exception:  # noqa: BLE001
                    continue
                price = smartprice_service.target_rub(cost, MARKUP, rate)
                v = Variant(
                    product_id=prod.id,
                    title=_variant_title(s.get("service_name"), cat_name)[:255],
                    liog_product_id=0, liog_variation_id=next_lvid,
                    cost_usd=cost, markup_percent=MARKUP, price_rub=price,
                    price_mode=smartprice_service.MODE_SMART,
                    source="nsgifts", fzr_kind="code",
                    fzr_a=cat_id, fzr_b=sid, is_active=False,  # номинал скрыт
                )
                session.add(v)
                existing_sids.add(sid)
                next_lvid -= 1
                n_var += 1
            await session.commit()

    return "\n".join([
        "✅ <b>Импорт NS.Gifts (коды/гифт-карты) завершён</b>",
        f"Категорий: <b>{n_cat}</b> (новых: {n_newcat})",
        f"Номиналов добавлено: <b>{n_var}</b>, пропущено (уже были): {n_skip}",
        "",
        f"Раздел «{SECTION_TITLE}» создан <b>скрытым</b>. Включайте нужные "
        "категории и номиналы в админке (цена — smart, наценка 9%).",
    ])

"""Импорт Valorant VP (гифт-карты FazerCard) в каталог по странам.

Структура, которую строит импортёр:

    Игры и Сервисы
      └ Valorant VP                 (раздел в конце, создаётся СКРЫТЫМ)
          └ 🇷🇺 Россия               (страна = категория «Valorant VP (RU)»)
              └ 240 VP              (номиналы категории)
              └ 475 VP …

Берёт все гифт-кард категории FazerCard с названием «Valorant VP (XX)», XX —
код страны. Наценка 9% (режим smart). Идемпотентно: повторный запуск добавляет
только новое (по (fzr_a, fzr_b) и по названию страны).
"""

from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Product, Variant
from . import rates as rates_service
from . import settings as settings_service
from . import smartprice as smartprice_service

log = logging.getLogger("vallshop.valorant")

SECTION_TITLE = "Valorant VP"
SECTION_PID_KEY = "valorant_section_pid"   # id раздела — чтобы переименование не ломало импорт
PARENT_TITLE = "Игры и Сервисы"
MARKUP = Decimal("9")                 # наценка 9% на все номиналы Valorant VP
_CODE_RE = re.compile(r"\(([A-Za-z]{2,5})\)")

# Код региона → (флаг, русское название). Что не распознаем — оставим код.
COUNTRY = {
    "AE": ("🇦🇪", "ОАЭ"), "AR": ("🇦🇷", "Аргентина"), "AU": ("🇦🇺", "Австралия"),
    "BH": ("🇧🇭", "Бахрейн"), "BR": ("🇧🇷", "Бразилия"), "CA": ("🇨🇦", "Канада"),
    "CL": ("🇨🇱", "Чили"), "CO": ("🇨🇴", "Колумбия"), "EU": ("🇪🇺", "Европа"),
    "GB": ("🇬🇧", "Великобритания"), "UK": ("🇬🇧", "Великобритания"),
    "HK": ("🇭🇰", "Гонконг"), "ID": ("🇮🇩", "Индонезия"), "IN": ("🇮🇳", "Индия"),
    "JP": ("🇯🇵", "Япония"), "KR": ("🇰🇷", "Корея"), "KW": ("🇰🇼", "Кувейт"),
    "MX": ("🇲🇽", "Мексика"), "MY": ("🇲🇾", "Малайзия"), "NZ": ("🇳🇿", "Новая Зеландия"),
    "OM": ("🇴🇲", "Оман"), "PE": ("🇵🇪", "Перу"), "PH": ("🇵🇭", "Филиппины"),
    "PK": ("🇵🇰", "Пакистан"), "QA": ("🇶🇦", "Катар"), "RU": ("🇷🇺", "Россия"),
    "SA": ("🇸🇦", "Саудовская Аравия"), "SG": ("🇸🇬", "Сингапур"),
    "TH": ("🇹🇭", "Таиланд"), "TR": ("🇹🇷", "Турция"), "TW": ("🇹🇼", "Тайвань"),
    "US": ("🇺🇸", "США"), "VN": ("🇻🇳", "Вьетнам"), "ZA": ("🇿🇦", "ЮАР"),
    "EG": ("🇪🇬", "Египет"), "NG": ("🇳🇬", "Нигерия"), "SE": ("🇸🇪", "Швеция"),
}


async def _fetch_valorant_cats(fzr) -> list:
    """Собрать категории «Valorant VP (…)» из гифт-карт FazerCard.

    Пагинируем сами безопасным размером страницы (у некоторых эндпоинтов
    limit=500 из all_categories отклоняется), фильтруем по названию.
    """
    out: list = []
    cursor = None
    for _ in range(80):  # защита от бесконечного цикла
        data = await asyncio.to_thread(fzr.list_giftcards, limit=200, cursor=cursor)
        items = data.get("items") if isinstance(data, dict) else None
        for it in items or []:
            name = str(it.get("name") or "")
            if name.strip().lower().startswith("valorant vp"):
                cid = it.get("category_id") or it.get("id") or it.get("slug")
                if cid:
                    out.append({"id": cid, "name": name})
        meta = (data.get("meta") if isinstance(data, dict) else None) or {}
        cursor = meta.get("next_cursor")
        if not cursor or not meta.get("has_more"):
            break
    return out


def _country_title(code: str, raw_name: str) -> str:
    """Название страны БЕЗ флага: «Россия»."""
    c = (code or "").upper()
    _flag, name = COUNTRY.get(c, ("", ""))
    if name:
        return name
    return raw_name or c or "—"   # незнакомый код → оставим как есть


def _country_flagged(code: str, raw_name: str) -> str:
    """Старый формат с флагом: «🇷🇺 Россия» — для поиска уже созданных."""
    c = (code or "").upper()
    flag, name = COUNTRY.get(c, ("", ""))
    if name:
        return f"{flag} {name}".strip()
    return raw_name or c or "—"


async def _games_services_id(session) -> Optional[int]:
    p = await session.scalar(
        select(Product).where(Product.title == PARENT_TITLE, Product.parent_id.is_(None)).limit(1)
    )
    return p.id if p else None


async def _find_existing_section(session, cat_ids: list) -> Optional[Product]:
    """Найти УЖЕ созданный раздел, даже если его переименовали.

    Порядок: по сохранённому id → по «деду» уже загруженного номинала
    (страна → её родитель = раздел) → по названию SECTION_TITLE.
    """
    # 1) запомненный id
    try:
        saved = await settings_service.get(session, SECTION_PID_KEY)
    except Exception:  # noqa: BLE001
        saved = None
    if saved:
        try:
            p = await session.get(Product, int(saved))
        except Exception:  # noqa: BLE001
            p = None
        if p is not None:
            return p
    # 2) дед любого уже импортированного номинала Valorant (устойчиво к переименованию)
    if cat_ids:
        v = await session.scalar(
            select(Variant).where(
                Variant.source == "fazercard",
                Variant.fzr_a.in_([str(c) for c in cat_ids]),
            ).limit(1)
        )
        if v is not None:
            country = await session.get(Product, v.product_id)
            if country is not None and country.parent_id is not None:
                sec = await session.get(Product, country.parent_id)
                if sec is not None:
                    return sec
    # 3) по названию (первый импорт / раздел не переименовывали)
    parent_id = await _games_services_id(session)
    return await session.scalar(
        select(Product).where(
            Product.title == SECTION_TITLE,
            (Product.parent_id == parent_id) if parent_id is not None else Product.parent_id.is_(None),
        ).limit(1)
    )


async def _ensure_section(session, cat_ids: Optional[list] = None) -> Product:
    sec = await _find_existing_section(session, cat_ids or [])
    if sec is not None:
        # запомним id, чтобы дальнейшие запуски не зависели от названия
        try:
            await settings_service.set(session, SECTION_PID_KEY, str(sec.id))
        except Exception:  # noqa: BLE001
            pass
        return sec
    parent_id = await _games_services_id(session)
    # В конце раздела «Игры и Сервисы».
    max_sort = await session.scalar(
        select(func.max(Product.sort_order)).where(
            (Product.parent_id == parent_id) if parent_id is not None else Product.parent_id.is_(None)
        )
    )
    sec = Product(
        game=SECTION_TITLE[:64], title=SECTION_TITLE[:255], parent_id=parent_id,
        sort_order=int(max_sort or 0) + 1, is_active=False,  # раздел скрыт до проверки
    )
    session.add(sec)
    await session.flush()
    try:
        await settings_service.set(session, SECTION_PID_KEY, str(sec.id))
    except Exception:  # noqa: BLE001
        pass
    return sec


async def _ensure_country(session, section: Product, name: str, old: str) -> tuple:
    """Найти/создать страну. Матчит по новому (без флага) ИЛИ старому (с флагом)
    названию; старое переименовывает в новое. Возвращает (product, created)."""
    child = await session.scalar(
        select(Product).where(
            Product.parent_id == section.id,
            Product.title.in_([name, old]),
        ).limit(1)
    )
    if child is not None:
        if child.title != name:        # был с флагом → убираем флаг
            child.title = name[:255]
            child.game = name[:64]
            await session.flush()
        return child, False
    max_sort = await session.scalar(
        select(func.max(Product.sort_order)).where(Product.parent_id == section.id)
    )
    child = Product(
        game=name[:64], title=name[:255], parent_id=section.id,
        sort_order=int(max_sort or 0) + 1, is_active=True,
    )
    session.add(child)
    await session.flush()
    return child, True


async def run_import(db: Database, fzr, config: BotConfig) -> str:
    """Импорт/досоздание дерева Valorant VP. Возвращает текст-отчёт."""
    if fzr is None or not (getattr(fzr, "mock", False) or getattr(fzr, "api_key", None)):
        return "FazerCard не настроен — импорт недоступен."
    try:
        val_cats = await _fetch_valorant_cats(fzr)
    except Exception as e:  # noqa: BLE001
        log.exception("valorant: не удалось получить категории")
        return f"Не удалось получить категории FazerCard:\n<code>{type(e).__name__}: {e}</code>"

    if not val_cats:
        return "Категории «Valorant VP» у FazerCard не найдены."

    rate = rates_service.get_rate()
    cat_ids = [str(c.get("id")) for c in val_cats if c.get("id") is not None]
    n_countries = n_new_countries = n_nominals = n_skipped = 0
    unknown: list = []

    async with db.session() as session:
        section = await _ensure_section(session, cat_ids)
        await session.commit()

    for c in val_cats:
        cat_id = str(c.get("id"))
        raw_name = str(c.get("name") or "")
        m = _CODE_RE.search(raw_name)
        code = (m.group(1).upper() if m else "")
        if code and code not in COUNTRY:
            unknown.append(code)
        name = _country_title(code, raw_name)
        old = _country_flagged(code, raw_name)
        try:
            offers = await asyncio.to_thread(fzr.offers_for, "giftcard", cat_id)
        except Exception:  # noqa: BLE001
            continue
        if not offers:
            continue

        async with db.session() as session:
            section = await _ensure_section(session, cat_ids)
            country, created = await _ensure_country(session, section, name, old)
            n_countries += 1
            if created:
                n_new_countries += 1
            for off in offers:
                oid = off.get("id")
                if oid is None:
                    continue
                existing = await session.scalar(
                    select(Variant).where(
                        Variant.source == "fazercard",
                        Variant.fzr_a == cat_id, Variant.fzr_b == str(oid),
                    )
                )
                if existing is not None:
                    if not existing.full_width:   # номиналы — в один столбик
                        existing.full_width = True
                    n_skipped += 1
                    continue
                try:
                    cost = Decimal(str(off.get("price_usd")))
                except Exception:  # noqa: BLE001
                    continue
                price = smartprice_service.target_rub(cost, MARKUP, rate)
                min_vid = await session.scalar(
                    select(func.min(Variant.liog_variation_id)).where(Variant.product_id == country.id)
                )
                new_lvid = min(0, int(min_vid or 0)) - 1
                v = Variant(
                    product_id=country.id, title=(off.get("name") or "VP")[:255],
                    liog_product_id=0, liog_variation_id=new_lvid,
                    cost_usd=cost, markup_percent=MARKUP, price_rub=price,
                    price_mode=smartprice_service.MODE_SMART,
                    source="fazercard", fzr_kind="giftcard",
                    fzr_a=cat_id, fzr_b=str(oid), is_active=True,
                    full_width=True,   # номиналы — в один столбик
                )
                session.add(v)
                n_nominals += 1
            await session.commit()

    lines = [
        "✅ <b>Импорт Valorant VP завершён</b>",
        f"Стран: <b>{n_countries}</b> (новых: {n_new_countries})",
        f"Номиналов добавлено: <b>{n_nominals}</b>, пропущено (уже были): {n_skipped}",
        "",
        "Раздел «Valorant VP» создан <b>скрытым</b> — проверьте дерево/цены и "
        "включите его в админке каталога.",
    ]
    if unknown:
        uniq = ", ".join(sorted(set(unknown)))
        lines.append(f"\n⚠️ Неизвестные коды стран (оставлены как есть, переименуйте): {uniq}")
    return "\n".join(lines)

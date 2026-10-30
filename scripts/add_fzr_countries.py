"""Завести подкатегории-страны и их номиналы из FazerCard одним заходом.

Пример (PlayStation): под товаром «Пополнение PlayStation» на каждую страну,
которую отдаёт FazerCard, создаётся подкатегория (русское название страны),
в неё добавляются ВСЕ номиналы, а их названия — только количество
(«25 USD», «500 TRY»), без префикса «PlayStation (XX) ·».

Идемпотентно: существующие подкатегории переиспользуются (по названию),
уже добавленные номиналы пропускаются (по fzr_b). Существующие названия
номиналов под этим товаром тоже чистятся до количества.

Запуск на сервере:
    # предпросмотр (без записи): что за страны, сколько номиналов, примеры названий
    docker compose exec bot python -m scripts.add_fzr_countries "Пополнение PlayStation" giftcard PlayStation 10 --dry
    # применить
    docker compose exec bot python -m scripts.add_fzr_countries "Пополнение PlayStation" giftcard PlayStation 10

Аргументы: <родитель(название или id)> <kind> <бренд> <наценка%> [--dry]
  kind  — giftcard | gamekey | topup
  бренд — по нему ищем категории FazerCard (напр. PlayStation, Google Play)
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from decimal import Decimal

from sqlalchemy import func, select

from bot.db import Database, Product
from bot.db.models import Variant
from bot.services import catalog as catalog_service
from fazercard import FazerCardClient, FazerCardError

# Код страны / английское название → русское название подкатегории.
# Ключи в нижнем регистре. Дополняется при необходимости (см. предупреждения --dry).
COUNTRY_RU = {
    "us": "США", "usa": "США", "united states": "США",
    "gb": "Великобритания", "uk": "Великобритания", "united kingdom": "Великобритания",
    "tr": "Турция", "turkey": "Турция",
    "in": "Индия", "india": "Индия",
    "de": "Германия", "germany": "Германия",
    "fr": "Франция", "france": "Франция",
    "it": "Италия", "italy": "Италия",
    "es": "Испания", "spain": "Испания",
    "be": "Бельгия", "belgium": "Бельгия",
    "nl": "Нидерланды", "netherlands": "Нидерланды",
    "pl": "Польша", "poland": "Польша",
    "pt": "Португалия", "portugal": "Португалия",
    "se": "Швеция", "sweden": "Швеция",
    "no": "Норвегия", "norway": "Норвегия",
    "fi": "Финляндия", "finland": "Финляндия",
    "dk": "Дания", "denmark": "Дания",
    "ie": "Ирландия", "ireland": "Ирландия",
    "at": "Австрия", "austria": "Австрия",
    "ch": "Швейцария", "switzerland": "Швейцария",
    "cz": "Чехия", "czech republic": "Чехия", "czechia": "Чехия",
    "sk": "Словакия", "slovakia": "Словакия",
    "hu": "Венгрия", "hungary": "Венгрия",
    "ro": "Румыния", "romania": "Румыния",
    "gr": "Греция", "greece": "Греция",
    "ua": "Украина", "ukraine": "Украина",
    "ru": "Россия", "russia": "Россия",
    "kz": "Казахстан", "kazakhstan": "Казахстан",
    "ae": "ОАЭ", "uae": "ОАЭ", "united arab emirates": "ОАЭ",
    "sa": "Саудовская Аравия", "saudi arabia": "Саудовская Аравия",
    "qa": "Катар", "qatar": "Катар",
    "kw": "Кувейт", "kuwait": "Кувейт",
    "bh": "Бахрейн", "bahrain": "Бахрейн",
    "om": "Оман", "oman": "Оман",
    "il": "Израиль", "israel": "Израиль",
    "eg": "Египет", "egypt": "Египет",
    "za": "ЮАР", "south africa": "ЮАР",
    "br": "Бразилия", "brazil": "Бразилия",
    "mx": "Мексика", "mexico": "Мексика",
    "ar": "Аргентина", "argentina": "Аргентина",
    "cl": "Чили", "chile": "Чили",
    "co": "Колумбия", "colombia": "Колумбия",
    "pe": "Перу", "peru": "Перу",
    "ca": "Канада", "canada": "Канада",
    "au": "Австралия", "australia": "Австралия",
    "nz": "Новая Зеландия", "new zealand": "Новая Зеландия",
    "jp": "Япония", "japan": "Япония",
    "kr": "Южная Корея", "south korea": "Южная Корея", "korea": "Южная Корея",
    "cn": "Китай", "china": "Китай",
    "hk": "Гонконг", "hong kong": "Гонконг",
    "tw": "Тайвань", "taiwan": "Тайвань",
    "sg": "Сингапур", "singapore": "Сингапур",
    "my": "Малайзия", "malaysia": "Малайзия",
    "th": "Таиланд", "thailand": "Таиланд",
    "id": "Индонезия", "indonesia": "Индонезия",
    "ph": "Филиппины", "philippines": "Филиппины",
    "vn": "Вьетнам", "vietnam": "Вьетнам",
    "pk": "Пакистан", "pakistan": "Пакистан",
    "bd": "Бангладеш", "bangladesh": "Бангладеш",
    "ng": "Нигерия", "nigeria": "Нигерия",
}


def _country_ru(token: str) -> tuple[str, bool]:
    """(русское_название, нашли_в_карте?). Если не нашли — вернём как есть."""
    t = (token or "").strip()
    ru = COUNTRY_RU.get(t.lower())
    if ru:
        return ru, True
    return t or "—", False


def _extract_country(cat_name: str, brand: str) -> str:
    """Выдернуть страну из названия категории FazerCard."""
    m = re.search(r"\(([^)]*)\)", cat_name or "")
    if m:
        return m.group(1).strip()
    rest = re.sub(re.escape(brand), "", cat_name or "", flags=re.IGNORECASE)
    return rest.strip(" -·•–—")


def _strip_brand(name: str, brand: str) -> str:
    """«PlayStation (US) · 25 USD» → «25 USD». «25 USD» → без изменений."""
    pat = re.compile(rf"^\s*{re.escape(brand)}\b\s*(?:\([^)]*\))?\s*(?:[·•\-–—]\s*)?", re.IGNORECASE)
    new = pat.sub("", name or "").strip()
    return new or (name or "")


async def _find_or_create_subcat(session, parent_id: int, title: str, dry: bool):
    row = await session.scalar(
        select(Product).where(Product.parent_id == parent_id, Product.title == title)
    )
    if row:
        return row, False
    if dry:
        return None, True
    maxs = await session.scalar(
        select(func.max(Product.sort_order)).where(Product.parent_id == parent_id)
    )
    p = Product(game="FazerCard", title=title, parent_id=parent_id, sort_order=int(maxs or 0) + 1)
    session.add(p)
    await session.flush()
    return p, True


async def run() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    if len(args) < 4:
        print(__doc__)
        return
    parent_ref, kind, brand, markup_raw = args[0], args[1], args[2], args[3]
    if kind not in ("giftcard", "gamekey", "topup"):
        print(f"kind должен быть giftcard|gamekey|topup, а не {kind!r}")
        return
    markup = Decimal(str(markup_raw).replace(",", ".").rstrip("%"))

    db = Database(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vallshop.db"))
    fzr = FazerCardClient.from_env()

    # родитель
    async with db.session() as session:
        if str(parent_ref).isdigit():
            parent = await session.get(Product, int(parent_ref))
        else:
            parent = await session.scalar(select(Product).where(Product.title == parent_ref))
        if not parent:
            print(f"✗ родительский товар не найден: {parent_ref!r}")
            await db.dispose()
            return
        parent_id = parent.id
        print(f"Родитель: «{parent.title}» id={parent_id}")

    # категории бренда из FazerCard
    try:
        cats = await asyncio.to_thread(fzr.all_categories, kind)
    except FazerCardError as e:
        print(f"✗ не удалось получить категории FazerCard: {e}")
        await db.dispose()
        return
    brand_cats = [c for c in cats if brand.lower() in str(c.get("name") or "").lower()]
    print(f"Категорий «{brand}» в FazerCard: {len(brand_cats)}\n")

    unmapped: set[str] = set()
    total_added = total_skipped = total_subcats = 0

    for c in brand_cats:
        token = _extract_country(c.get("name") or "", brand)
        ru, found = _country_ru(token)
        if not found and token:
            unmapped.add(token)
        try:
            offers = await asyncio.to_thread(fzr.offers_for, kind, c["id"])
        except FazerCardError as e:
            print(f"  ✗ {c.get('name')}: ошибка номиналов: {e}")
            continue
        offers = [o for o in offers if o.get("id")]
        for o in offers:                       # чистим имя оффера до количества
            o["name"] = _strip_brand(o.get("name") or "", brand)
        if not offers:
            print(f"  • {ru} ({token}) — номиналов нет, пропуск")
            continue

        async with db.session() as session:
            sub, created = await _find_or_create_subcat(session, parent_id, ru, dry)
            if created:
                total_subcats += 1
            tag = "＋новая" if created else "есть"
            if dry:
                sample = ", ".join(o["name"] for o in offers[:5])
                more = "…" if len(offers) > 5 else ""
                print(f"  • {ru} [{tag}] ← {c.get('name')}: {len(offers)} номиналов: {sample}{more}")
                continue
            added, skipped = await catalog_service.bulk_create_fzr_variants(
                session, sub.id, kind, {"id": c["id"], "name": ru}, offers, markup, name_only=True,
            )
            total_added += added
            total_skipped += skipped
            print(f"  • {ru} [{tag}] ← {c.get('name')}: +{added}, пропущено {skipped}")

    # почистить названия существующих номиналов под этим товаром (все подкатегории)
    if not dry:
        async with db.session() as session:
            subs = list(await session.scalars(select(Product).where(Product.parent_id == parent_id)))
            fixed = 0
            for sub in subs:
                variants = list(await session.scalars(select(Variant).where(Variant.product_id == sub.id)))
                for v in variants:
                    nn = _strip_brand(v.title, brand)
                    if nn != v.title:
                        v.title = nn
                        fixed += 1
            if fixed:
                await session.commit()
            print(f"\nПочищено названий номиналов: {fixed}")

    await db.dispose()
    if unmapped:
        print("\n⚠ не нашёл в карте стран (назову как есть — пришли перевод, добавлю): "
              + ", ".join(sorted(unmapped)))
    if dry:
        print("\n(dry-run) ничего не записано. Убери --dry, чтобы применить.")
    else:
        print(f"\nИтого: подкатегорий создано {total_subcats}, номиналов добавлено "
              f"{total_added}, пропущено {total_skipped}.")


if __name__ == "__main__":
    asyncio.run(run())

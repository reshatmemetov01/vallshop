"""Массовое добавление номиналов FazerCard по списку (без кликанья в боте).

Идея: один раз описываешь, какие товары и из каких категорий FazerCard
наполнить, и запускаешь одной командой. Идемпотентно — повторный запуск не
создаёт дубликатов (сверка по fzr_b), список можно дополнять и запускать снова.

Что делает по каждой строке списка:
  1) находит товар по названию или id (при желании создаёт, если нет);
  2) берёт номиналы категории FazerCard (по умолчанию — ВСЕ);
  3) отбирает нужные (only/exclude), считает цену = закуп × (1 + наценка%);
  4) добавляет номиналы к товару (уже существующие пропускает).

Запуск на сервере:
    docker compose exec bot python -m scripts.add_fzr            # по списку SPEC ниже
    docker compose exec bot python -m scripts.add_fzr --dry      # показать, что будет, без записи
    docker compose exec bot python -m scripts.add_fzr spec.json  # список из JSON-файла

ID категорий, если не знаешь, покажет:
    docker compose exec bot python -m scripts.fzr_catalog topups

Формат строки списка (поля):
    product   — название товара (точное) или его числовой id
    kind      — "giftcard" | "gamekey" | "topup"
    category  — category_id/game_id FazerCard (надёжнее всего) ИЛИ часть названия
    markup    — наценка в % к закупу (число)
    only      — (необязательно) список подстрок: брать только совпавшие номиналы
    exclude   — (необязательно) список подстрок: исключить совпавшие номиналы
    create    — (необязательно) True: создать товар верхнего уровня, если его нет
    parent    — (необязательно) название/ id родительской категории для нового товара
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal

from sqlalchemy import select

from bot.db import Database, Product
from bot.services import catalog as catalog_service
from fazercard import FazerCardClient, FazerCardError

# ── СПИСОК НОМИНАЛОВ ─────────────────────────────────────────────────────────
# Заполни своими строками. Пример (закомментирован):
SPEC: list[dict] = [
    # {"product": "PUBG Mobile UC", "kind": "topup", "category": "pubg_mobile",
    #  "markup": 20, "create": True},
    # {"product": "Steam Казахстан", "kind": "giftcard", "category": "steam",
    #  "markup": 15, "only": ["10 USD", "25 USD"]},
]


def _load_spec() -> list[dict]:
    """Список из JSON-файла (argv), иначе из SPEC в этом файле."""
    for a in sys.argv[1:]:
        if a.endswith(".json"):
            with open(a, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else data.get("items", [])
    return SPEC


def _match(name: str, needles: list[str]) -> bool:
    low = (name or "").lower()
    return any(n.lower() in low for n in needles)


async def _resolve_product(session, spec: dict):
    """Найти товар по id/названию; при create=True — создать верхнего уровня."""
    ref = spec.get("product")
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        p = await session.get(Product, int(ref))
        return p
    rows = list(await session.scalars(select(Product).where(Product.title == ref)))
    if rows:
        if len(rows) > 1:
            print(f"   ⚠ товаров с названием «{ref}» несколько ({len(rows)}), беру id={rows[0].id}")
        return rows[0]
    if not spec.get("create"):
        return None
    parent_id = None
    parent_ref = spec.get("parent")
    if parent_ref is not None:
        if isinstance(parent_ref, int) or (isinstance(parent_ref, str) and str(parent_ref).isdigit()):
            parent_id = int(parent_ref)
        else:
            pr = await session.scalar(select(Product).where(Product.title == parent_ref))
            parent_id = pr.id if pr else None
    p = Product(game="FazerCard", title=str(ref), parent_id=parent_id, sort_order=0)
    session.add(p)
    await session.flush()
    print(f"   + создан товар «{ref}» id={p.id}" + (f" (в разделе {parent_id})" if parent_id else ""))
    return p


def _resolve_category(fzr: FazerCardClient, kind: str, ref: str, cache: dict) -> dict | None:
    """Вернуть {id,name} категории: сначала по точному id, затем по части названия."""
    if kind not in cache:
        try:
            cache[kind] = fzr.all_categories(kind)
        except FazerCardError as e:
            print(f"   ⚠ не удалось получить список категорий {kind}: {e}")
            cache[kind] = []
    cats = cache[kind]
    ref_s = str(ref)
    for c in cats:
        if str(c.get("id")) == ref_s:
            return {"id": c["id"], "name": c.get("name") or ref_s}
    hits = [c for c in cats if ref_s.lower() in str(c.get("name") or "").lower()]
    if len(hits) == 1:
        return {"id": hits[0]["id"], "name": hits[0].get("name") or ref_s}
    if len(hits) > 1:
        names = ", ".join(f"{c.get('name')}[{c.get('id')}]" for c in hits[:6])
        print(f"   ⚠ по «{ref}» несколько категорий: {names} … уточни category (id)")
        return None
    # не нашли в списке — пробуем как прямой id (вдруг список неполный)
    return {"id": ref, "name": ref_s}


async def run() -> None:
    dry = "--dry" in sys.argv
    spec = _load_spec()
    if not spec:
        print("Список пуст. Заполни SPEC в scripts/add_fzr.py или передай spec.json.")
        return

    db = Database(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vallshop.db"))
    await db.create_all()
    fzr = FazerCardClient.from_env()
    cat_cache: dict = {}
    total_added = total_skipped = 0

    for idx, entry in enumerate(spec, 1):
        kind = entry.get("kind")
        head = f"[{idx}/{len(spec)}] {entry.get('product')} · {kind} · {entry.get('category')}"
        print(f"\n{head}")
        if kind not in ("giftcard", "gamekey", "topup"):
            print(f"   ✗ неизвестный kind: {kind}")
            continue
        try:
            markup = Decimal(str(entry.get("markup", 0)))
        except Exception:  # noqa: BLE001
            print(f"   ✗ некорректная наценка: {entry.get('markup')}")
            continue

        async with db.session() as session:
            product = await _resolve_product(session, entry)
            if not product:
                print("   ✗ товар не найден (добавь create: True, чтобы создать)")
                continue

            cat = _resolve_category(fzr, kind, entry.get("category"), cat_cache)
            if not cat:
                continue
            try:
                offers = await asyncio.to_thread(fzr.offers_for, kind, cat["id"])
            except FazerCardError as e:
                print(f"   ✗ ошибка FazerCard: {e}")
                continue
            offers = [o for o in offers if o.get("id")]

            only, exclude = entry.get("only") or [], entry.get("exclude") or []
            if only:
                offers = [o for o in offers if _match(o.get("name") or "", only)]
            if exclude:
                offers = [o for o in offers if not _match(o.get("name") or "", exclude)]
            if not offers:
                print("   ✗ под фильтр не попал ни один номинал")
                continue

            if dry:
                print(f"   (dry) добавил бы {len(offers)} (наценка +{markup}%):")
                for o in offers:
                    print(f"       · {o.get('name')}  ${o.get('price_usd')}")
                continue

            added, skipped = await catalog_service.bulk_create_fzr_variants(
                session, product.id, kind, cat, offers, markup
            )
            total_added += added
            total_skipped += skipped
            print(f"   ✅ добавлено {added}, пропущено (уже были) {skipped} · наценка +{markup}%")

    await db.dispose()
    if not dry:
        print(f"\nИтого: добавлено {total_added}, пропущено {total_skipped}.")


if __name__ == "__main__":
    asyncio.run(run())

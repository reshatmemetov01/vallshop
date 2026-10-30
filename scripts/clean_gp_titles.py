"""Почистить названия номиналов Google Play: убрать префикс «Google Play (XX) ·».

Было:  «Google Play (US) · 5 USD»   →  стало:  «5 USD»
       «Google Play (TR) · 25 TRY»  →          «25 TRY»

Затрагивает ТОЛЬКО номиналы, чьё название начинается с «Google Play»
(то есть только Google Play, другие товары не трогаются). Идемпотентно:
после чистки название уже не начинается с «Google Play», повторный запуск
ничего не меняет.

Запуск на сервере:
    docker compose exec bot python -m scripts.clean_gp_titles            # предпросмотр (без записи)
    docker compose exec bot python -m scripts.clean_gp_titles --apply    # применить
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from sqlalchemy import select

from bot.db import Database
from bot.db.models import Variant

# «Google Play» + необязательные «(US)» + необязательный разделитель (· • - – —)
_PREFIX_RE = re.compile(r"^\s*Google Play\b\s*(?:\([^)]*\))?\s*(?:[·•\-–—]\s*)?", re.IGNORECASE)


def _clean(title: str) -> str:
    new = _PREFIX_RE.sub("", title or "").strip()
    return new or title  # не оставляем пустое название


async def run() -> None:
    apply = "--apply" in sys.argv
    db = Database(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///vallshop.db"))

    async with db.session() as session:
        variants = list(
            await session.scalars(select(Variant).where(Variant.title.like("Google Play%")))
        )
        changed = 0
        for v in variants:
            new = _clean(v.title)
            if new != v.title:
                print(f"  #{v.id}: «{v.title}»  →  «{new}»")
                if apply:
                    v.title = new
                changed += 1
        if apply and changed:
            await session.commit()

    await db.dispose()
    if not variants:
        print("Номиналов Google Play не найдено.")
    elif apply:
        print(f"\n✅ Переименовано: {changed} из {len(variants)}.")
    else:
        print(f"\nБудет переименовано: {changed} из {len(variants)}. "
              "Запусти с --apply, чтобы применить.")


if __name__ == "__main__":
    asyncio.run(run())

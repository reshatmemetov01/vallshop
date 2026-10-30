"""Тест ops-скрипта массового добавления номиналов FazerCard (scripts/add_fzr)."""

import json

import pytest
from sqlalchemy import select

from bot.db import Database, Product
from bot.db.models import Variant
from scripts import add_fzr


def test_match_substring():
    assert add_fzr._match("25 USD", ["25 usd"]) is True
    assert add_fzr._match("10 USD", ["25", "50"]) is False
    assert add_fzr._match("", ["x"]) is False


class _FakeFzr:
    """Отдаёт пару категорий — для проверки резолва по id и по названию."""

    def all_categories(self, kind):
        return [{"id": "steam", "name": "Steam"}, {"id": "amazon_us", "name": "Amazon (US)"}]


def test_resolve_category_by_id_and_name():
    fzr, cache = _FakeFzr(), {}
    assert add_fzr._resolve_category(fzr, "giftcard", "steam", cache) == {"id": "steam", "name": "Steam"}
    # по части названия (регистр не важен)
    assert add_fzr._resolve_category(fzr, "giftcard", "amazon", cache)["id"] == "amazon_us"
    # нет в списке — пробуем как прямой id
    assert add_fzr._resolve_category(fzr, "giftcard", "unknown_id", cache)["id"] == "unknown_id"


@pytest.mark.asyncio
async def test_run_adds_and_is_idempotent(tmp_path, monkeypatch):
    dbfile = tmp_path / "t.db"
    url = f"sqlite+aiosqlite:///{dbfile}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("FZR_MOCK", "1")
    monkeypatch.setenv("FZR_API_KEY", "x")

    # посев товара
    db = Database(url)
    await db.create_all()
    async with db.session() as s:
        s.add(Product(game="Steam", title="Steam Гифткарты", sort_order=0))
        await s.commit()
    await db.dispose()

    spec = [{"product": "Steam Гифткарты", "kind": "giftcard", "category": "steam", "markup": 20}]
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["add_fzr", str(spec_file)])

    await add_fzr.run()

    db = Database(url)
    async with db.session() as s:
        variants = list(await s.scalars(select(Variant)))
    await db.dispose()
    assert len(variants) == 2                       # обе mock-карты (gc10, gc25)
    titles = {v.title for v in variants}
    assert "Steam · 10 USD" in titles
    # цена = закуп × 1.20 (наценка 20%)
    gc10 = next(v for v in variants if v.fzr_b == "gc10")
    assert str(gc10.price_usd) == "10.20"
    assert gc10.source == "fazercard"

    # повторный запуск не задваивает
    await add_fzr.run()
    db = Database(url)
    async with db.session() as s:
        again = list(await s.scalars(select(Variant)))
    await db.dispose()
    assert len(again) == 2

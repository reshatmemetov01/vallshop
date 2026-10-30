"""Тест скрипта авто-подкатегорий по странам (scripts/add_fzr_countries)."""

import pytest
from sqlalchemy import select

from bot.db import Database, Product
from bot.db.models import Variant
from scripts import add_fzr_countries as mod


def test_extract_and_map_country():
    assert mod._country_ru(mod._extract_country("PlayStation (US)", "PlayStation")) == ("США", True)
    assert mod._country_ru(mod._extract_country("PlayStation (Turkey)", "PlayStation")) == ("Турция", True)
    assert mod._country_ru(mod._extract_country("PlayStation Germany", "PlayStation")) == ("Германия", True)
    # незнакомая — возвращаем как есть, mapped=False
    ru, ok = mod._country_ru(mod._extract_country("PlayStation (Freedonia)", "PlayStation"))
    assert ok is False and ru == "Freedonia"


def test_strip_brand():
    assert mod._strip_brand("PlayStation (US) · 25 USD", "PlayStation") == "25 USD"
    assert mod._strip_brand("PlayStation (DE) 50 EUR", "PlayStation") == "50 EUR"
    assert mod._strip_brand("25 USD", "PlayStation") == "25 USD"
    assert mod._strip_brand("PlayStation (US)", "PlayStation") == "PlayStation (US)"  # не оставляем пусто


@pytest.mark.asyncio
async def test_run_creates_subcats_and_cleans(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path/'t.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("FZR_MOCK", "1")
    monkeypatch.setenv("FZR_API_KEY", "x")

    db = Database(url)
    await db.create_all()
    async with db.session() as s:
        s.add(Product(game="X", title="Пополнение Amazon", sort_order=0))
        await s.commit()
    await db.dispose()

    # mock FazerCard отдаёт категорию «Amazon (US)» с картами 10/25 USD
    monkeypatch.setattr("sys.argv", ["x", "Пополнение Amazon", "giftcard", "Amazon", "10"])
    await mod.run()

    db = Database(url)
    async with db.session() as s:
        sub = await s.scalar(select(Product).where(Product.title == "США"))
        assert sub is not None and sub.parent_id is not None
        variants = list(await s.scalars(select(Variant).where(Variant.product_id == sub.id)))
    await db.dispose()
    assert {v.title for v in variants} == {"10 USD", "25 USD"}     # только количество
    gc10 = next(v for v in variants if v.fzr_b == "gc10")
    assert str(gc10.price_usd) == "9.35"                          # $8.50 × 1.10
    assert gc10.source == "fazercard"

    # повторный запуск не дублирует
    monkeypatch.setattr("sys.argv", ["x", "Пополнение Amazon", "giftcard", "Amazon", "10"])
    await mod.run()
    db = Database(url)
    async with db.session() as s:
        again = list(await s.scalars(select(Variant)))
    await db.dispose()
    assert len(again) == 2

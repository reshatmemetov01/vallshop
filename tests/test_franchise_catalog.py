"""Франшизы: скрытие «Активировать Код» и head_only-товаров из каталога."""

import pytest

from bot.db import Database
from bot.db.models import Product
from bot.services import catalog as catalog_service
from bot.services import menu as menu_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    await menu_service.load(database)
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_activate_button_head_only(db):
    # «Активировать Код» (main.activate) — только в головном боте.
    head_keys = [r.key for r in menu_service.menu_entries("main", 0)]
    fr_keys = [r.key for r in menu_service.menu_entries("main", 1)]
    assert "main.activate" in head_keys
    assert "main.activate" not in fr_keys
    # и в редакторе оформления франшизы её тоже нет (only_enabled=False)
    fr_all = [r.key for r in menu_service.menu_entries("main", 1, only_enabled=False)]
    assert "main.activate" not in fr_all


@pytest.mark.asyncio
async def test_head_only_product_hidden_for_franchise(db):
    async with db.session() as s:
        roblox = Product(game="ROBLOX", title="Roblox"); s.add(roblox); await s.flush()
        cards = Product(game="ROBLOX", title="Roblox Карты", parent_id=roblox.id)
        gamepass = Product(game="ROBLOX", title="Roblox Game Pass", parent_id=roblox.id,
                           head_only=True)
        s.add(cards); s.add(gamepass)
        await s.commit()
        rid = roblox.id

    async with db.session() as s:
        # головной каталог видит обе подкатегории
        head = await catalog_service.list_children(s, rid, exclude_head_only=False)
        assert {p.title for p in head} == {"Roblox Карты", "Roblox Game Pass"}
        # франшиза — без head_only товара
        fr = await catalog_service.list_children(s, rid, exclude_head_only=True)
        assert {p.title for p in fr} == {"Roblox Карты"}


@pytest.mark.asyncio
async def test_head_only_top_level(db):
    async with db.session() as s:
        s.add(Product(game="X", title="Обычный"))
        s.add(Product(game="Y", title="Секрет", head_only=True))
        await s.commit()
    async with db.session() as s:
        head = await catalog_service.list_products(s, exclude_head_only=False)
        fr = await catalog_service.list_products(s, exclude_head_only=True)
    assert {p.title for p in head} == {"Обычный", "Секрет"}
    assert {p.title for p in fr} == {"Обычный"}

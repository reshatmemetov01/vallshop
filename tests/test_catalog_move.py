"""Перестановка порядка кнопок каталога (move_product)."""

import pytest

from bot.db import Database, Product
from bot.services import catalog as catalog_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _titles(db, parent_id=None):
    async with db.session() as s:
        ps = await catalog_service.list_products(s, only_active=False, parent_id=parent_id)
    return [p.title for p in ps]


@pytest.mark.asyncio
async def test_move_down_and_up(db):
    async with db.session() as s:
        # все sort_order=0 (дефолт) — порядок по id
        s.add_all([Product(game="g", title="A"), Product(game="g", title="B"),
                   Product(game="g", title="C")])
        await s.commit()
        ids = {p.title: p.id for p in await catalog_service.list_products(s, only_active=False)}
    assert await _titles(db) == ["A", "B", "C"]

    async with db.session() as s:
        assert await catalog_service.move_product(s, ids["A"], +1) is True
    assert await _titles(db) == ["B", "A", "C"]

    async with db.session() as s:
        assert await catalog_service.move_product(s, ids["C"], -1) is True
    assert await _titles(db) == ["B", "C", "A"]


@pytest.mark.asyncio
async def test_move_bounds(db):
    async with db.session() as s:
        s.add_all([Product(game="g", title="A"), Product(game="g", title="B")])
        await s.commit()
        ids = {p.title: p.id for p in await catalog_service.list_products(s, only_active=False)}
    async with db.session() as s:
        assert await catalog_service.move_product(s, ids["A"], -1) is False  # уже вверху
    async with db.session() as s:
        assert await catalog_service.move_product(s, ids["B"], +1) is False  # уже внизу
    assert await _titles(db) == ["A", "B"]


@pytest.mark.asyncio
async def test_move_only_within_level(db):
    async with db.session() as s:
        cat = Product(game="g", title="Категория")
        s.add(cat)
        await s.flush()
        s.add_all([Product(game="g", title="X", parent_id=cat.id),
                   Product(game="g", title="Y", parent_id=cat.id)])
        top = Product(game="g", title="Верхний")
        s.add(top)
        await s.commit()
        cid = cat.id
        xy = {p.title: p.id for p in await catalog_service.list_products(s, only_active=False, parent_id=cid)}
    # перестановка внутри подкатегории не трогает верхний уровень
    async with db.session() as s:
        assert await catalog_service.move_product(s, xy["X"], +1) is True
    assert await _titles(db, parent_id=cid) == ["Y", "X"]
    assert await _titles(db) == ["Категория", "Верхний"]

"""Перестановка порядка кнопок каталога (move_product) и номиналов (move_variant)."""

from decimal import Decimal

import pytest

from bot.db import Database, Product
from bot.db.models import Variant
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


async def _vtitles(db, pid):
    async with db.session() as s:
        vs = await catalog_service.list_variants(s, pid, only_active=False)
    return [v.title for v in vs]


@pytest.mark.asyncio
async def test_move_variant(db):
    async with db.session() as s:
        p = Product(game="g", title="P")
        s.add(p)
        await s.flush()
        for t in ("N1", "N2", "N3"):
            s.add(Variant(product_id=p.id, title=t, liog_product_id=0,
                          liog_variation_id=-hash(t) % 1000 - 1, price_rub=Decimal("1")))
        await s.commit()
        pid = p.id
        ids = {v.title: v.id for v in await catalog_service.list_variants(s, pid, only_active=False)}
    assert await _vtitles(db, pid) == ["N1", "N2", "N3"]
    async with db.session() as s:
        assert await catalog_service.move_variant(s, ids["N1"], +1) is True
    assert await _vtitles(db, pid) == ["N2", "N1", "N3"]
    async with db.session() as s:
        assert await catalog_service.move_variant(s, ids["N2"], -1) is False  # уже вверху

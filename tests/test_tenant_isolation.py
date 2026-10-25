"""Изоляция магазинов: head_only-номиналы и накрутка чужих кликов."""

import pytest

from bot.db import Database
from bot.db.models import Broadcast, BroadcastClick, Product, Variant
from bot.services import broadcasts as broadcasts_service
from bot.services import catalog as catalog_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_get_variant_head_only_hidden_for_franchise(db):
    async with db.session() as s:
        p = Product(game="g", title="Эксклюзив", head_only=True)
        s.add(p)
        await s.flush()
        v = Variant(product_id=p.id, title="ном", liog_product_id=0, liog_variation_id=1)
        s.add(v)
        await s.commit()
        vid = v.id
    async with db.session() as s:
        # головной (exclude_head_only=False) — видит
        assert await catalog_service.get_variant(s, vid) is not None
        assert await catalog_service.get_variant(s, vid, exclude_head_only=False) is not None
        # франшиза (exclude_head_only=True) — номинал скрыт даже по прямому id
        assert await catalog_service.get_variant(s, vid, exclude_head_only=True) is None


@pytest.mark.asyncio
async def test_get_variant_normal_visible_for_franchise(db):
    async with db.session() as s:
        p = Product(game="g", title="Обычный", head_only=False)
        s.add(p)
        await s.flush()
        v = Variant(product_id=p.id, title="ном", liog_product_id=0, liog_variation_id=1)
        s.add(v)
        await s.commit()
        vid = v.id
    async with db.session() as s:
        assert await catalog_service.get_variant(s, vid, exclude_head_only=True) is not None


async def _broadcast(db, merchant_id):
    async with db.session() as s:
        bc = Broadcast(merchant_id=merchant_id, created_by=1, text="t", status="sent")
        s.add(bc)
        await s.commit()
        return bc.id


async def _clicks(db, bid):
    async with db.session() as s:
        rows = list(await s.scalars(
            __import__("sqlalchemy").select(BroadcastClick).where(
                BroadcastClick.broadcast_id == bid
            )
        ))
    return sum(r.count for r in rows)


@pytest.mark.asyncio
async def test_record_click_rejects_foreign_merchant(db):
    bid = await _broadcast(db, merchant_id=5)  # рассылка франшизы #5
    # клиент другого магазина (#7) пытается накрутить чужой счётчик
    await broadcasts_service.record_click(db, bid, 0, 100, merchant_id=7)
    assert await _clicks(db, bid) == 0
    # свой магазин — засчитывается
    await broadcasts_service.record_click(db, bid, 0, 100, merchant_id=5)
    assert await _clicks(db, bid) == 1
    # без merchant_id (старое поведение) — тоже засчитывается
    await broadcasts_service.record_click(db, bid, 0, 101)
    assert await _clicks(db, bid) == 2

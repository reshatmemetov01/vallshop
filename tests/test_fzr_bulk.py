"""Пакетное добавление номиналов FazerCard: общая наценка, порядок, пропуск дублей."""

from decimal import Decimal

import pytest

from bot.db import Database
from bot.db.models import Product, Variant
from bot.services import catalog as catalog_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _product(db):
    async with db.session() as s:
        p = Product(game="GC", title="Xbox Gift Cards UK")
        s.add(p)
        await s.commit()
        return p.id


CAT = {"id": 777, "name": "Xbox Gift Cards UK"}


def _offer(oid, name, price):
    return {"id": oid, "name": name, "price_usd": price}


@pytest.mark.asyncio
async def test_bulk_create_price_order_and_markup(db):
    pid = await _product(db)
    offers = [
        _offer(10, "£5", "6.00"),
        _offer(20, "£10", "12.00"),
        _offer(30, "£25", "30.00"),
    ]
    async with db.session() as s:
        added, skipped = await catalog_service.bulk_create_fzr_variants(
            s, pid, "giftcard", CAT, offers, Decimal("20")
        )
    assert (added, skipped) == (3, 0)

    async with db.session() as s:
        variants = await catalog_service.list_variants(s, pid, only_active=False)
    # порядок в каталоге = порядок выбора
    assert [v.fzr_b for v in variants] == ["10", "20", "30"]
    # цена = закуп × 1.20, в USD (плавает по курсу); sort_order возрастает
    assert variants[0].cost_usd == Decimal("6.00")
    assert variants[0].price_usd == Decimal("7.20")
    assert variants[1].price_usd == Decimal("14.40")
    assert variants[2].price_usd == Decimal("36.00")
    assert [v.sort_order for v in variants] == sorted(v.sort_order for v in variants)
    assert variants[0].sort_order < variants[1].sort_order < variants[2].sort_order
    # все — FazerCard giftcard с уникальными отрицательными liog_variation_id
    assert all(v.source == "fazercard" and v.fzr_kind == "giftcard" for v in variants)
    vids = [v.liog_variation_id for v in variants]
    assert len(set(vids)) == 3 and all(x < 0 for x in vids)


@pytest.mark.asyncio
async def test_bulk_skips_already_added(db):
    pid = await _product(db)
    # номинал £10 (offer 20) уже добавлен ранее
    async with db.session() as s:
        s.add(Variant(
            product_id=pid, title="£10", liog_product_id=0, liog_variation_id=-1,
            source="fazercard", fzr_kind="giftcard", fzr_a="777", fzr_b="20",
            cost_usd=Decimal("12.00"), sort_order=5,
        ))
        await s.commit()
    offers = [_offer(10, "£5", "6.00"), _offer(20, "£10", "12.00"), _offer(30, "£25", "30.00")]
    async with db.session() as s:
        added, skipped = await catalog_service.bulk_create_fzr_variants(
            s, pid, "giftcard", CAT, offers, Decimal("0")
        )
    assert (added, skipped) == (2, 1)
    async with db.session() as s:
        variants = await catalog_service.list_variants(s, pid, only_active=False)
    assert sorted(v.fzr_b for v in variants) == ["10", "20", "30"]
    # новые идут после уже существующего (sort_order=5)
    new = [v for v in variants if v.fzr_b in ("10", "30")]
    assert all(v.sort_order > 5 for v in new)
    # наценка 0% → цена = закуп
    v5 = next(v for v in variants if v.fzr_b == "10")
    assert v5.price_usd == Decimal("6.00")


@pytest.mark.asyncio
async def test_bulk_empty_and_all_duplicates_commit_nothing(db):
    pid = await _product(db)
    async with db.session() as s:
        s.add(Variant(
            product_id=pid, title="£5", liog_product_id=0, liog_variation_id=-1,
            source="fazercard", fzr_kind="giftcard", fzr_a="777", fzr_b="10",
            cost_usd=Decimal("6.00"),
        ))
        await s.commit()
    async with db.session() as s:
        added, skipped = await catalog_service.bulk_create_fzr_variants(
            s, pid, "giftcard", CAT, [_offer(10, "£5", "6.00")], Decimal("50")
        )
    assert (added, skipped) == (0, 1)
    async with db.session() as s:
        assert len(await catalog_service.list_variants(s, pid, only_active=False)) == 1

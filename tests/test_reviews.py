"""Отзывы: создание, модерация, публичный список."""

from decimal import Decimal

import pytest

from bot.db import Database
from bot.services import reviews as review_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _add(db, text="хорошо", rating=5, publish=False):
    async with db.session() as s:
        r = await review_service.create_review(
            s, order_id=1, user_id=7, username="buyer",
            variant_id=10, product_title="60 UC", amount_usd=Decimal("5"),
            rating=rating, text=text,
        )
        rid = r.id
        if publish:
            await review_service.set_status(s, rid, "published")
        await s.commit()
        return rid


@pytest.mark.asyncio
async def test_create_is_new_and_hidden_from_public(db):
    await _add(db)
    async with db.session() as s:
        assert await review_service.count_published(s) == 0
        assert await review_service.list_published(s) == []


@pytest.mark.asyncio
async def test_publish_then_appears(db):
    rid = await _add(db, publish=True)
    async with db.session() as s:
        assert await review_service.count_published(s) == 1
        pub = await review_service.list_published(s)
        assert len(pub) == 1 and pub[0].id == rid


@pytest.mark.asyncio
async def test_edit_text(db):
    rid = await _add(db)
    async with db.session() as s:
        await review_service.update_text(s, rid, "новый текст")
        await s.commit()
    async with db.session() as s:
        r = await review_service.get(s, rid)
        assert r.text == "новый текст"


@pytest.mark.asyncio
async def test_delete(db):
    rid = await _add(db, publish=True)
    async with db.session() as s:
        await review_service.delete(s, rid)
        await s.commit()
    async with db.session() as s:
        assert await review_service.get(s, rid) is None
        assert await review_service.count_published(s) == 0


@pytest.mark.asyncio
async def test_pagination(db):
    for i in range(7):
        await _add(db, text=f"r{i}", publish=True)
    async with db.session() as s:
        assert await review_service.count_published(s) == 7
        p0 = await review_service.list_published(s, page=0)
        p1 = await review_service.list_published(s, page=1)
        assert len(p0) == review_service.PAGE_SIZE  # 5
        assert len(p1) == 2


# ── Изоляция по франшизам: модерация своих отзывов ───────────────────────────
async def _add_m(db, merchant_id, publish=False, product="товар"):
    async with db.session() as s:
        r = await review_service.create_review(
            s, order_id=1, user_id=7, username="b",
            variant_id=1, product_title=product, amount_usd=Decimal("5"),
            rating=5, text="ок", merchant_id=merchant_id,
        )
        rid = r.id
        if publish:
            await review_service.set_status(s, rid, "published")
        await s.commit()
        return rid


@pytest.mark.asyncio
async def test_list_all_scoped_by_merchant(db):
    await _add_m(db, 0, product="head")
    await _add_m(db, 5, product="fr5-a")
    await _add_m(db, 5, product="fr5-b")
    async with db.session() as s:
        # головной (None) видит все
        assert len(await review_service.list_all(s, merchant_id=None)) == 3
        # франшиза 5 — только свои
        fr = await review_service.list_all(s, merchant_id=5)
        assert len(fr) == 2 and {r.product_title for r in fr} == {"fr5-a", "fr5-b"}
        # франшиза без отзывов
        assert await review_service.list_all(s, merchant_id=9) == []


@pytest.mark.asyncio
async def test_count_published_scoped(db):
    await _add_m(db, 0, publish=True)
    await _add_m(db, 5, publish=True)
    await _add_m(db, 5, publish=False)
    async with db.session() as s:
        assert await review_service.count_published(s, None) == 2   # все магазины
        assert await review_service.count_published(s, 0) == 1      # головной
        assert await review_service.count_published(s, 5) == 1      # франшиза


@pytest.mark.asyncio
async def test_in_scope_isolation(db):
    from bot.handlers.admin_reviews import _in_scope, _scope
    from bot.db.models import HEAD_MERCHANT_ID
    head_rid = await _add_m(db, 0)
    fr_rid = await _add_m(db, 5)
    assert _scope(HEAD_MERCHANT_ID) is None       # головной — все
    assert _scope(5) == 5                          # франшиза — свой id
    async with db.session() as s:
        # головной (scope None) видит любой отзыв
        assert await _in_scope(s, fr_rid, None) is not None
        assert await _in_scope(s, head_rid, None) is not None
        # франшиза 5 видит свой, но НЕ чужой (головной)
        assert await _in_scope(s, fr_rid, 5) is not None
        assert await _in_scope(s, head_rid, 5) is None

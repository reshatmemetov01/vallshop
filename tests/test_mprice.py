"""Ценовой слой мерчанта (P1.1): наценка, фикс, стоп-лосс, пакетный расчёт."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant
from bot.services import mprice


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _merchant(db, default_markup="0"):
    async with db.session() as s:
        m = Merchant(title="A", bot_token="t:x", bot_username="a",
                     admin_user_id=1, default_markup=Decimal(default_markup))
        s.add(m)
        await s.commit()
        return m.id


@pytest.mark.asyncio
async def test_head_price_unchanged(db):
    async with db.session() as s:
        assert await mprice.client_price(s, 0, 1, Decimal("100")) == Decimal("100")


@pytest.mark.asyncio
async def test_default_markup(db):
    mid = await _merchant(db, "20")
    async with db.session() as s:
        # 100 × 1.20 = 120
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("120")


@pytest.mark.asyncio
async def test_variant_markup_override(db):
    mid = await _merchant(db, "20")
    async with db.session() as s:
        await mprice.set_markup(s, mid, 1, Decimal("50"))
        await s.commit()
        # индивидуальная наценка 50% важнее дефолтной 20%
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("150")
        assert await mprice.client_price(s, mid, 2, Decimal("100")) == Decimal("120")  # другой — дефолт


@pytest.mark.asyncio
async def test_fixed_and_stop_loss(db):
    mid = await _merchant(db, "0")
    async with db.session() as s:
        await mprice.set_fixed(s, mid, 1, Decimal("200"))
        await s.commit()
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("200")
        # фикс ниже цены владельца → стоп-лосс поднимает до цены владельца
        await mprice.set_fixed(s, mid, 1, Decimal("80"))
        await s.commit()
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("100")


@pytest.mark.asyncio
async def test_batch_prices(db):
    mid = await _merchant(db, "10")
    async with db.session() as s:
        await mprice.set_fixed(s, mid, 2, Decimal("250"))
        await s.commit()
        out = await mprice.client_prices(s, mid, {1: Decimal("100"), 2: Decimal("100")})
    assert out == {1: Decimal("110"), 2: Decimal("250")}


@pytest.mark.asyncio
async def test_clear_override(db):
    mid = await _merchant(db, "10")
    async with db.session() as s:
        await mprice.set_markup(s, mid, 1, Decimal("50"))
        await s.commit()
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("150")
        await mprice.clear_override(s, mid, 1)
        await s.commit()
        assert await mprice.client_price(s, mid, 1, Decimal("100")) == Decimal("110")  # вернулась дефолтная

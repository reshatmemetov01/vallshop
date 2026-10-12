"""Фаза 1 франшизы: таблица merchants, колонка merchant_id, миграция старой БД."""

import sqlite3

import pytest
from sqlalchemy import text

from bot.db import Database, HEAD_MERCHANT_ID, Merchant, Order, TopUp, User
from bot.db.models import Product, Variant


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_head_merchant_seeded(db):
    async with db.session() as s:
        head = await s.get(Merchant, HEAD_MERCHANT_ID)
    assert head is not None
    assert head.id == 0
    assert head.status == "active"
    assert head.title  # какое-то название задано


@pytest.mark.asyncio
async def test_new_merchant_gets_positive_id(db):
    async with db.session() as s:
        m = Merchant(title="Shop A", admin_user_id=123, admin_username="partner")
        s.add(m)
        await s.commit()
        assert m.id > 0  # не затирает головной (0)


@pytest.mark.asyncio
async def test_user_defaults_to_head_merchant(db):
    async with db.session() as s:
        s.add(User(id=1, username="buyer"))
        await s.commit()
    async with db.session() as s:
        u = await s.get(User, 1)
        assert u.merchant_id == HEAD_MERCHANT_ID == 0


@pytest.mark.asyncio
async def test_order_and_topup_have_merchant_id(db):
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="v", liog_product_id=0, liog_variation_id=-1)
        s.add(v); s.add(User(id=2, username="u2")); await s.flush()
        s.add(Order(client_ref="o-1", user_id=2, variant_id=v.id, price_usd=100))
        s.add(TopUp(client_ref="tu-1", user_id=2, amount_usd=100))
        await s.commit()
    async with db.session() as s:
        o = (await s.execute(text("SELECT merchant_id FROM orders"))).scalars().all()
        t = (await s.execute(text("SELECT merchant_id FROM topups"))).scalars().all()
    assert o == [0] and t == [0]


@pytest.mark.asyncio
async def test_migration_adds_merchant_id_to_legacy_db(tmp_path):
    # Старая БД: таблица users без merchant_id и без таблицы merchants.
    dbfile = tmp_path / "legacy.db"
    con = sqlite3.connect(dbfile)
    con.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64), "
        "balance NUMERIC(12,2) NOT NULL DEFAULT 0);"
        "INSERT INTO users (id, username) VALUES (777, 'old');"
    )
    con.commit()
    con.close()

    database = Database(f"sqlite+aiosqlite:///{dbfile}")
    await database.create_all()  # должна до-мигрировать
    try:
        async with database.session() as s:
            cols = [r[1] for r in (await s.execute(text("PRAGMA table_info(users)"))).all()]
            assert "merchant_id" in cols
            # существующая запись получила merchant_id = 0
            mid = (await s.execute(text("SELECT merchant_id FROM users WHERE id=777"))).scalar()
            assert mid == 0
            # таблица merchants создана и головной засеян
            head = (await s.execute(text("SELECT id, status FROM merchants WHERE id=0"))).first()
            assert head == (0, "active")
    finally:
        await database.dispose()

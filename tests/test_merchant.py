"""Фаза 1 франшизы: таблица merchants, колонка merchant_id, миграция старой БД."""

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import text

from bot.db import Database, HEAD_MERCHANT_ID, Merchant, Order, TopUp, User
from bot.db.models import Product, Variant
from bot.services import balance as bal


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
        u = await s.get(User, {"id": 1, "merchant_id": 0})
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
async def test_same_user_two_merchants_separate_balance(db):
    # Один Telegram id у двух мерчантов — отдельные записи и балансы.
    async with db.session() as s:
        s.add(User(id=5, merchant_id=0, balance=Decimal("100")))
        s.add(User(id=5, merchant_id=1, balance=Decimal("0")))
        await s.commit()
    async with db.session() as s:
        await bal.credit(s, 5, Decimal("30"), merchant_id=1)
        await s.commit()
    async with db.session() as s:
        assert await bal.get_balance(s, 5, merchant_id=0) == Decimal("100")
        assert await bal.get_balance(s, 5, merchant_id=1) == Decimal("30")
        # списание у мерчанта 1 не трогает баланс головного
        assert await bal.try_debit(s, 5, Decimal("20"), merchant_id=1) is True
        await s.commit()
        assert await bal.get_balance(s, 5, merchant_id=1) == Decimal("10")
        assert await bal.get_balance(s, 5, merchant_id=0) == Decimal("100")


@pytest.mark.asyncio
async def test_migration_rebuilds_users_composite_pk(tmp_path):
    # Старая users с одностолбцовым ключом id → перестраивается в составной.
    dbfile = tmp_path / "legacy2.db"
    con = sqlite3.connect(dbfile)
    con.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64), "
        "balance NUMERIC(12,2) NOT NULL DEFAULT 0);"
        "INSERT INTO users (id, username, balance) VALUES (42, 'old', 500);"
    )
    con.commit()
    con.close()

    database = Database(f"sqlite+aiosqlite:///{dbfile}")
    await database.create_all()
    try:
        async with database.session() as s:
            info = (await s.execute(text("PRAGMA table_info(users)"))).all()
            pk = {r[1] for r in info if r[5] > 0}  # r[5] = pk-позиция
            assert pk == {"id", "merchant_id"}
            row = (await s.execute(
                text("SELECT username, balance, merchant_id FROM users WHERE id=42")
            )).first()
            assert row[0] == "old" and int(row[1]) == 500 and row[2] == 0
            # тот же id у другого мерчанта теперь сосуществует
            await s.execute(text(
                "INSERT INTO users (id, merchant_id, username) VALUES (42, 1, 'franchise')"
            ))
            await s.commit()
            cnt = (await s.execute(text("SELECT COUNT(*) FROM users WHERE id=42"))).scalar()
            assert cnt == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_purchase_scoped_to_merchant(db):
    from bot.services import orders as order_service
    from bot.services import stock as stock_service
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="code", liog_product_id=1, liog_variation_id=2,
                    cost_usd=Decimal("1.00"))
        s.add(v); await s.flush()
        await stock_service.add_codes(s, v.id, ["CODE-1"])
        # один и тот же Telegram id у двух мерчантов, у каждого свой баланс
        s.add(User(id=7, merchant_id=0, balance=Decimal("100")))
        s.add(User(id=7, merchant_id=1, balance=Decimal("100")))
        await s.commit()
        vid = v.id
    async with db.session() as s:
        v = await s.get(Variant, vid)
        order, codes = await order_service.purchase_from_balance(
            s, user_id=7, variant=v, unit_price=Decimal("50"), quantity=1, merchant_id=1,
        )
        await s.commit()
        assert order.merchant_id == 1 and codes == ["CODE-1"]
    async with db.session() as s:
        # списалось у мерчанта 1, баланс головного не тронут
        assert await bal.get_balance(s, 7, merchant_id=1) == Decimal("50")
        assert await bal.get_balance(s, 7, merchant_id=0) == Decimal("100")


@pytest.mark.asyncio
async def test_active_merchants_registry(db):
    from bot.services import merchants as ms
    async with db.session() as s:
        s.add(Merchant(id=1, title="A", bot_token="111:AAA", status="active"))
        s.add(Merchant(id=2, title="B", bot_token=None, status="active"))          # нет токена
        s.add(Merchant(id=3, title="C", bot_token="333:CCC", status="suspended"))  # не активен
        await s.commit()
    rows = await ms.active_merchants(db)
    assert sorted(m.id for m in rows) == [1]  # головной (0), без токена и неактивные — вне списка


class _FakeBot:
    def __init__(self, bot_id):
        self.id = bot_id


@pytest.mark.asyncio
async def test_merchant_middleware_injects_id():
    from bot.middlewares import MerchantMiddleware
    mw = MerchantMiddleware({111: 0, 222: 7})
    seen = {}

    async def handler(event, data):
        seen.update(data)
        return "ok"

    assert await mw(handler, object(), {"bot": _FakeBot(222)}) == "ok"
    assert seen["merchant_id"] == 7          # известный бот франшизы
    seen.clear()
    await mw(handler, object(), {"bot": _FakeBot(999)})
    assert seen["merchant_id"] == 0          # неизвестный бот → головной
    seen.clear()
    await mw(handler, object(), {})
    assert seen["merchant_id"] == 0          # нет бота → головной


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

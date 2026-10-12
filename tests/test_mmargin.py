"""Маржа мерчанта (P1.2): начисление при покупке и сторно при возврате."""

from decimal import Decimal

import pytest

from bot.db import Database, HEAD_MERCHANT_ID, Merchant, User
from bot.db.models import Product, Variant
from bot.services import balance as bal
from bot.services import mprice
from bot.services import orders as order_service
from bot.services import stock as stock_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _setup(db, *, markup="20", codes=1):
    """Товар со стоком + мерчант с наценкой + покупатель с балансом у обоих."""
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="code", liog_product_id=1, liog_variation_id=2,
                    cost_usd=Decimal("1.00"))
        s.add(v); await s.flush()
        await stock_service.add_codes(s, v.id, [f"CODE-{i}" for i in range(1, codes + 1)])
        m = Merchant(title="A", bot_token="t:x", bot_username="a",
                     admin_user_id=1, default_markup=Decimal(markup))
        s.add(m); await s.flush()
        s.add(User(id=7, merchant_id=0, balance=Decimal("1000")))
        s.add(User(id=7, merchant_id=m.id, balance=Decimal("1000")))
        await s.commit()
        return v.id, m.id


@pytest.mark.asyncio
async def test_margin_accrued_on_purchase(db):
    vid, mid = await _setup(db, markup="20")
    async with db.session() as s:
        v = await s.get(Variant, vid)
        owner = Decimal("50")
        client = await mprice.client_price(s, mid, vid, owner)  # 50 × 1.20 = 60
        assert client == Decimal("60")
        order, codes = await order_service.purchase_from_balance(
            s, user_id=7, variant=v, unit_price=client, quantity=1,
            merchant_id=mid, owner_unit_price=owner,
        )
        await s.commit()
        assert codes == ["CODE-1"]
        assert order.merchant_margin == Decimal("10")
    async with db.session() as s:
        # клиент заплатил цену клиента (60), баланс головного не тронут
        assert await bal.get_balance(s, 7, merchant_id=mid) == Decimal("940")
        assert await bal.get_balance(s, 7, merchant_id=0) == Decimal("1000")
        # маржа (10) начислена на баланс заработка мерчанта
        m = await s.get(Merchant, mid)
        assert m.earnings_rub == Decimal("10")


@pytest.mark.asyncio
async def test_head_purchase_has_zero_margin(db):
    vid, _mid = await _setup(db)
    async with db.session() as s:
        v = await s.get(Variant, vid)
        # головной: owner == client, маржа 0
        order, _codes = await order_service.purchase_from_balance(
            s, user_id=7, variant=v, unit_price=Decimal("50"), quantity=1,
            merchant_id=HEAD_MERCHANT_ID, owner_unit_price=Decimal("50"),
        )
        await s.commit()
        assert order.merchant_margin == Decimal("0")
        head = await s.get(Merchant, HEAD_MERCHANT_ID)
        assert Decimal(head.earnings_rub or 0) == Decimal("0")


@pytest.mark.asyncio
async def test_margin_reversed_on_cancel(db):
    vid, mid = await _setup(db, markup="20")
    async with db.session() as s:
        v = await s.get(Variant, vid)
        order, _codes = await order_service.purchase_from_balance(
            s, user_id=7, variant=v, unit_price=Decimal("60"), quantity=1,
            merchant_id=mid, owner_unit_price=Decimal("50"),
        )
        await s.commit()
        oid = order.id
    async with db.session() as s:
        order = await s.get(order_service.Order, oid)
        refund = await order_service.cancel_order_manual(s, order)
        await s.commit()
        assert refund == Decimal("60")
    async with db.session() as s:
        # деньги вернулись покупателю этого мерчанта, маржа сторнирована
        assert await bal.get_balance(s, 7, merchant_id=mid) == Decimal("1000")
        m = await s.get(Merchant, mid)
        assert m.earnings_rub == Decimal("0")


@pytest.mark.asyncio
async def test_margin_quantity_multiplies(db):
    vid, mid = await _setup(db, markup="50", codes=3)
    async with db.session() as s:
        v = await s.get(Variant, vid)
        # owner 100 → client 150, 3 шт → маржа 150
        order, codes = await order_service.purchase_from_balance(
            s, user_id=7, variant=v, unit_price=Decimal("150"), quantity=3,
            merchant_id=mid, owner_unit_price=Decimal("100"),
        )
        await s.commit()
        assert len(codes) == 3
        assert order.merchant_margin == Decimal("150")
        m = await s.get(Merchant, mid)
        assert m.earnings_rub == Decimal("150")


@pytest.mark.asyncio
async def test_accrue_and_reverse_helpers(db):
    async with db.session() as s:
        m = Merchant(title="A", bot_token="t:y", bot_username="a", admin_user_id=1)
        s.add(m); await s.flush()
        mid = m.id
        await mprice.accrue_earnings(s, mid, Decimal("30"))
        await mprice.accrue_earnings(s, mid, Decimal("20"))
        await s.commit()
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("50")
        # сторно не уходит ниже нуля
        await mprice.reverse_earnings(s, mid, Decimal("70"))
        await s.commit()
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("0")
        # головной не накапливает маржу
        await mprice.accrue_earnings(s, HEAD_MERCHANT_ID, Decimal("99"))
        await s.commit()
        assert Decimal((await s.get(Merchant, HEAD_MERCHANT_ID)).earnings_rub or 0) == Decimal("0")


@pytest.mark.asyncio
async def test_markup_price_ignores_fixed(db):
    vid, mid = await _setup(db, markup="20")
    async with db.session() as s:
        # фикс-override не применяется к динамической цене (Steam/Stars) — только %
        await mprice.set_fixed(s, mid, vid, Decimal("999"))
        await s.commit()
        # markup_price берёт дефолтную наценку 20% (фикс игнорируется)
        assert await mprice.markup_price(s, mid, vid, Decimal("100")) == Decimal("120")
        # а client_price (каталог) учёл бы фикс
        assert await mprice.client_price(s, mid, vid, Decimal("100")) == Decimal("999")

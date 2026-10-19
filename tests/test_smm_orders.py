"""SMM-заказ: оформление у soc-proof и урегулирование (выполнен/частично/отмена)."""

from decimal import Decimal

import pytest

from bot.db import Database
from bot.db.models import Order, OrderStatus, Product, Variant
from bot.services import balance as balance_service
from bot.services import orders as order_service
from bot.services import smm


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


class FakeSoc:
    """Мини-клиент soc-proof для тестов (только add/status)."""
    configured = True

    def __init__(self, status_resp=None, order_id=555, add_raises=None):
        self._status = status_resp or {}
        self._order_id = order_id
        self._add_raises = add_raises
        self.added = []

    def add(self, *, service, link, quantity):
        if self._add_raises:
            raise self._add_raises
        self.added.append((service, link, quantity))
        return {"order": self._order_id}

    def status(self, order):
        return self._status


async def _setup(db, rate="9.00", markup="50", balance="1000"):
    async with db.session() as s:
        p = Product(game="SMM", title="TG")
        s.add(p)
        await s.flush()
        v = Variant(
            product_id=p.id, title="Подписчики", liog_product_id=0, liog_variation_id=-1,
            source="socproof", smm_service=707, smm_min=10, smm_max=60000,
            smm_rate=Decimal(rate), markup_percent=Decimal(markup),
        )
        s.add(v)
        await s.flush()
        await order_service.ensure_user(s, 1, "u", "U", 0)
        await balance_service.credit(s, 1, Decimal(balance), merchant_id=0)
        await s.commit()
        return v.id


async def _buy(db, vid, qty=1000, soc=None):
    """Купить SMM-услугу с баланса (head merchant)."""
    async with db.session() as s:
        v = await s.get(Variant, vid)
        owner_1000 = smm.sell_per_1000_rub(v.smm_rate, v.markup_percent)  # 13.5
        total = smm.order_total_rub(owner_1000, qty)
        order, _ = await order_service.purchase_from_balance(
            s, user_id=1, variant=v, unit_price=total, quantity=1, merchant_id=0,
            owner_unit_price=total, soc=soc, smm_link="https://t.me/x", smm_quantity=qty,
        )
        await s.commit()
        return order.id, total


@pytest.mark.asyncio
async def test_purchase_creates_fulfilling_order(db):
    vid = await _setup(db)
    soc = FakeSoc(order_id=999)
    oid, total = await _buy(db, vid, qty=1000, soc=soc)
    assert total == Decimal("13.50")          # 9 ×1.5 ×1000/1000
    assert soc.added == [(707, "https://t.me/x", 1000)]
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.status == OrderStatus.FULFILLING
        assert o.supplier == "socproof"
        assert o.supplier_order_id == "999"
        assert o.quantity == 1            # SMM: qty=1, реальное кол-во в fields
        # баланс списан на total
        assert await balance_service.get_balance(s, 1, 0) == Decimal("1000") - total


@pytest.mark.asyncio
async def test_settle_completed(db):
    vid = await _setup(db)
    oid, total = await _buy(db, vid, qty=1000, soc=FakeSoc(order_id=1))
    soc = FakeSoc(status_resp={"status": "Completed", "remains": "0"})
    async with db.session() as s:
        o = await s.get(Order, oid)
        res = await order_service.settle_socproof(s, o, soc)
        await s.commit()
    assert res["outcome"] == "completed"
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.status == OrderStatus.COMPLETED
        # деньги не возвращались
        assert await balance_service.get_balance(s, 1, 0) == Decimal("1000") - total


@pytest.mark.asyncio
async def test_settle_partial_refund(db):
    vid = await _setup(db)
    oid, total = await _buy(db, vid, qty=1000, soc=FakeSoc(order_id=1))
    # доставлено 600 из 1000 → remains 400 → возврат 40% от total
    soc = FakeSoc(status_resp={"status": "Partial", "remains": "400"})
    async with db.session() as s:
        o = await s.get(Order, oid)
        res = await order_service.settle_socproof(s, o, soc)
        await s.commit()
    assert res["outcome"] == "partial"
    expected_refund = (total * Decimal(400) / Decimal(1000)).quantize(Decimal("0.01"))
    assert res["refund"] == expected_refund
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.status == OrderStatus.COMPLETED
        assert "частично" in (o.fail_reason or "")
        assert await balance_service.get_balance(s, 1, 0) == Decimal("1000") - total + expected_refund


@pytest.mark.asyncio
async def test_settle_canceled_full_refund(db):
    vid = await _setup(db)
    oid, total = await _buy(db, vid, qty=1000, soc=FakeSoc(order_id=1))
    soc = FakeSoc(status_resp={"status": "Canceled", "remains": "1000"})
    async with db.session() as s:
        o = await s.get(Order, oid)
        res = await order_service.settle_socproof(s, o, soc)
        await s.commit()
    assert res["outcome"] == "canceled"
    assert res["refund"] == total
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.status == OrderStatus.REFUNDED
        # полный возврат
        assert await balance_service.get_balance(s, 1, 0) == Decimal("1000")


@pytest.mark.asyncio
async def test_settle_in_progress_updates_fields(db):
    vid = await _setup(db)
    oid, total = await _buy(db, vid, qty=1000, soc=FakeSoc(order_id=1))
    import json
    soc = FakeSoc(status_resp={"status": "In progress", "remains": "250"})
    async with db.session() as s:
        o = await s.get(Order, oid)
        res = await order_service.settle_socproof(s, o, soc)
        await s.commit()
    assert res["outcome"] == "progress"
    assert res["progress"] == 75   # (1000-250)/1000
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.status == OrderStatus.FULFILLING
        f = json.loads(o.fields_json)
        assert f["remains"] == 250 and f["progress"] == 75
        # баланс не трогали
        assert await balance_service.get_balance(s, 1, 0) == Decimal("1000") - total

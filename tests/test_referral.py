"""Реферальная программа: привязка, начисление 5%, идемпотентность, сторно, перевод."""

from decimal import Decimal

import pytest

from bot.db import Database, Product, Variant
from bot.db.models import Order, OrderStatus, User
from bot.services import balance as balance_service
from bot.services import orders as order_service
from bot.services import referral as referral_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _mk_order(s, buyer_id, price="100", status=OrderStatus.COMPLETED):
    p = Product(game="g", title="t")
    s.add(p); await s.flush()
    v = Variant(product_id=p.id, title="v", liog_product_id=0, liog_variation_id=0, cost_usd=Decimal("1"))
    s.add(v); await s.flush()
    o = Order(client_ref=f"r{buyer_id}-{price}", user_id=buyer_id, variant_id=v.id,
              price_usd=Decimal(price), quantity=1, status=status)
    s.add(o); await s.flush()
    return o


@pytest.mark.asyncio
async def test_bind_only_new_not_self(db):
    async with db.session() as s:
        ref = User(id=1, username="ref")
        newu = User(id=2, username="new")
        s.add_all([ref, newu]); await s.flush()
        assert await referral_service.bind_referral(s, newu, 1) is True
        # повторно — нельзя переназначить
        assert await referral_service.bind_referral(s, newu, 1) is False
        # на себя — нельзя
        self_u = User(id=3); s.add(self_u); await s.flush()
        assert await referral_service.bind_referral(s, self_u, 3) is False


@pytest.mark.asyncio
async def test_accrue_5_percent_and_idempotent(db):
    async with db.session() as s:
        s.add(User(id=1, username="ref"))
        s.add(User(id=2, username="buyer", referred_by=1))
        await s.flush()
        o = await _mk_order(s, 2, "100")
        await referral_service.accrue_for_order(s, o)
        await referral_service.accrue_for_order(s, o)  # повтор — без дубля
        await s.commit()
        ref = await s.get(User, {"id": 1, "merchant_id": 0})
        assert ref.ref_balance_rub == Decimal("5.00")
        assert ref.ref_earned_rub == Decimal("5.00")


@pytest.mark.asyncio
async def test_no_accrue_without_referrer(db):
    async with db.session() as s:
        s.add(User(id=2, username="buyer"))  # без referred_by
        await s.flush()
        o = await _mk_order(s, 2, "100")
        await referral_service.accrue_for_order(s, o)
        await s.commit()
        # никаких начислений
        st = await referral_service.admin_stats(s)
        assert st["paid_usdt"] == Decimal("0")


@pytest.mark.asyncio
async def test_reverse_on_refund(db):
    async with db.session() as s:
        s.add(User(id=1, username="ref"))
        s.add(User(id=2, username="buyer", referred_by=1))
        await s.flush()
        o = await _mk_order(s, 2, "100")
        await referral_service.accrue_for_order(s, o)
        await s.commit()
        assert (await s.get(User, {"id": 1, "merchant_id": 0})).ref_balance_rub == Decimal("5.00")
        o.status = OrderStatus.REFUNDED
        await referral_service.reverse_for_order(s, o)
        await s.commit()
        assert (await s.get(User, {"id": 1, "merchant_id": 0})).ref_balance_rub == Decimal("0.00")


@pytest.mark.asyncio
async def test_transfer_min_and_success(db):
    async with db.session() as s:
        u = User(id=1, username="ref", ref_balance_rub=Decimal("0.50"))
        s.add(u); await s.flush()
        ok, bal, need = await referral_service.transfer_to_balance(s, 1, "RUB")
        assert ok is False and need == Decimal("50")  # ниже минимума
        u.ref_balance_rub = Decimal("60.00")
        await s.flush()
        ok, amount, _ = await referral_service.transfer_to_balance(s, 1, "RUB")
        await s.commit()
        assert ok and amount == Decimal("60.00")
        assert (await s.get(User, {"id": 1, "merchant_id": 0})).ref_balance_rub == Decimal("0.00")
        assert await balance_service.get_balance(s, 1) == Decimal("60.00")


@pytest.mark.asyncio
async def test_stats_counts(db):
    async with db.session() as s:
        s.add(User(id=1, username="ref"))
        s.add(User(id=2, referred_by=1))
        s.add(User(id=3, referred_by=1))
        await s.flush()
        o = await _mk_order(s, 2, "50")
        await referral_service.accrue_for_order(s, o)
        await s.commit()
        st = await referral_service.stats(s, 1)
        assert st["invited"] == 2
        assert st["with_purchase"] == 1
        assert st["bal_rub"] == Decimal("2.50")

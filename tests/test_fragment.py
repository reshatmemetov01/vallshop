"""Fragment как источник звёзд: маршрутизация, фолбэк на FazerCard, подтверждение."""

from decimal import Decimal

import pytest

from bot.db import Database, Product, Variant
from bot.db.models import Order, OrderStatus, User
from bot.services import balance as balance_service
from bot.services import orders as order_service
from fazercard import FazerCardClient
from fragment import FragmentClient, FragmentError


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _stars_variant(db):
    async with db.session() as s:
        s.add(User(id=5))
        await balance_service.credit(s, 5, Decimal("1000"))
        p = Product(game="TG_STARS", title="Звёзды")
        s.add(p)
        await s.flush()
        v = Variant(product_id=p.id, title="Звёзды", liog_product_id=0, liog_variation_id=-1,
                    cost_usd=Decimal("0"), source="fazercard", fzr_kind="stars",
                    fzr_a="telegram_stars")
        s.add(v)
        await s.flush()
        await s.commit()
        return v.id


async def _buy(db, vid, *, fzr, fragment):
    async with db.session() as s:
        v = await s.get(Variant, vid)
        order, codes = await order_service.purchase_from_balance(
            s, user_id=5, variant=v, unit_price=Decimal("160"), quantity=1,
            fzr=fzr, fragment=fragment,
            topup_fields={"telegram_username": "someuser", "stars_qty": 100, "_cost_usd": "1.5"},
        )
        await s.commit()
        return order.id


def test_client_mock_basics():
    c = FragmentClient(mock=True)
    assert c.configured
    r = c.buy_stars(username="@u", quantity=100, idempotency_key="k")
    assert r["ok"] and r["status"] == "completed"
    assert c.poll(r)["status"] == "completed"
    # без куки/сида — не настроен
    assert FragmentClient().configured is False


@pytest.mark.asyncio
async def test_stars_go_through_fragment(db):
    vid = await _stars_variant(db)
    oid = await _buy(db, vid, fzr=FazerCardClient(mock=True), fragment=FragmentClient(mock=True))
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.supplier == "fragment"            # ушло в Fragment, не в FazerCard
        assert o.status == OrderStatus.COMPLETED    # mock отдаёт completed сразу
        assert await balance_service.get_balance(s, 5) == Decimal("840.00")


class _BrokenFragment:
    configured = True

    def buy_stars(self, **kw):
        raise FragmentError("куки протухли")


@pytest.mark.asyncio
async def test_fallback_to_fazercard_when_fragment_fails(db):
    vid = await _stars_variant(db)
    # Fragment падает → должен сработать фолбэк на FazerCard (mock = completed)
    oid = await _buy(db, vid, fzr=FazerCardClient(mock=True), fragment=_BrokenFragment())
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.supplier == "fazercard"
        assert o.status == OrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_fragment_not_configured_uses_fazercard(db):
    vid = await _stars_variant(db)
    oid = await _buy(db, vid, fzr=FazerCardClient(mock=True), fragment=FragmentClient())  # не настроен
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.supplier == "fazercard"


class _PendingFragment:
    configured = True

    def __init__(self):
        self._done = False

    def buy_stars(self, **kw):
        return {"ok": True, "order": "ext-hash", "status": "pending", "seqno_at_send": 1}

    def poll(self, state):
        return {"status": "completed" if self._done else "pending"}


@pytest.mark.asyncio
async def test_settle_fragment_confirms_pending(db):
    vid = await _stars_variant(db)
    frag = _PendingFragment()
    oid = await _buy(db, vid, fzr=FazerCardClient(mock=True), fragment=frag)
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.supplier == "fragment" and o.status == OrderStatus.FULFILLING  # ждём оплату
    # первая проверка — ещё pending
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert (await order_service.settle_fragment(s, o, frag))["outcome"] == "progress"
    # оплата подтвердилась
    frag._done = True
    async with db.session() as s:
        o = await s.get(Order, oid)
        res = await order_service.settle_fragment(s, o, frag)
        await s.commit()
        assert res["outcome"] == "completed" and o.status == OrderStatus.COMPLETED

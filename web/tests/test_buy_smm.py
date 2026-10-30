"""Покупка SMM (накрутка) на сайте: ссылка + количество, цена за 1000."""

from decimal import Decimal

import pytest

from bot.db.base import Database
from bot.db.models import HEAD_MERCHANT_ID, OrderStatus, Product, User, Variant
from web.app import db as web_db
from web.app.services import accounts as account_service
from web.app.services import checkout as checkout_service
from web.app.services import orders_view as ov
from web.app.services import wallet as wallet_service


class _FakeSoc:
    configured = True

    def add(self, *, service, link, quantity):
        return {"order": 777}


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    checkout_service._sup()
    fake = _FakeSoc()
    checkout_service._suppliers["soc"] = fake
    checkout_service._suppliers["panels"]["socproof"] = fake   # покупка берёт панель по source
    yield database
    web_db.database = old
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    await database.dispose()


async def _smm_variant(s, rate="10", mn=100, mx=10000) -> int:
    p = Product(game="SMM", title="SMM"); s.add(p); await s.flush()
    v = Variant(product_id=p.id, title="Лайки", liog_product_id=0, liog_variation_id=-1,
                source="socproof", smm_service=123, smm_rate=Decimal(rate),
                smm_min=mn, smm_max=mx)
    s.add(v); await s.flush()
    return v.id


async def _account(s, email="a@a.com", balance="1000"):
    acc = await account_service.register(s, email, "secret12")
    wid = await wallet_service.ensure_wallet(s, acc)
    w = await s.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
    w.balance = Decimal(balance)
    return acc


def test_buy_kind_smm():
    v = Variant(product_id=1, title="t", liog_product_id=0, liog_variation_id=-1,
                source="socproof")
    assert checkout_service.buy_kind(v) == "smm"


@pytest.mark.asyncio
async def test_buy_smm(db):
    async with db.session() as s:
        vid = await _smm_variant(s, rate="10")      # 10 ₽ / 1000
        acc = await _account(s, "smm@smm.com", "1000")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        res = await checkout_service.purchase(
            s, acc, vid, 2000, {"link": "https://t.me/channel/5"}
        )
        assert res["status"] == OrderStatus.FULFILLING.value
        assert res["kind"] == "pending"
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        bal = await wallet_service.get_balance(s, acc)
        assert bal == Decimal("980")                # 1000 − 10×2000/1000 = 20
        det = await ov.order_detail(s, acc, res["order_id"])
        assert det["smm"]["quantity"] == 2000
        assert det["smm"]["link"] == "https://t.me/channel/5"


@pytest.mark.asyncio
async def test_buy_smm_bad_link(db):
    async with db.session() as s:
        vid = await _smm_variant(s)
        acc = await _account(s, "b@b.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="ссылку"):
            await checkout_service.purchase(s, acc, vid, 500, {"link": "не ссылка"})


@pytest.mark.asyncio
async def test_buy_smm_qty_out_of_limits(db):
    async with db.session() as s:
        vid = await _smm_variant(s, mn=100, mx=1000)
        acc = await _account(s, "c@c.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError):
            await checkout_service.purchase(s, acc, vid, 50, {"link": "https://t.me/x"})
        with pytest.raises(checkout_service.CheckoutError):
            await checkout_service.purchase(s, acc, vid, 5000, {"link": "https://t.me/x"})


@pytest.mark.asyncio
async def test_buy_smm_insufficient(db):
    async with db.session() as s:
        vid = await _smm_variant(s, rate="100")      # 100 ₽ / 1000
        acc = await _account(s, "d@d.com", "50")     # 100×2000/1000 = 200 > 50
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="Недостаточно"):
            await checkout_service.purchase(s, acc, vid, 2000, {"link": "https://t.me/x"})
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        assert await wallet_service.get_balance(s, acc) == Decimal("50")

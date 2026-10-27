"""Покупка с баланса на сайте: сток (мгновенно), лимиты, типы, авторизация."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import (
    HEAD_MERCHANT_ID,
    OrderStatus,
    Product,
    User,
    Variant,
)
from bot.services import stock as stock_service
from web.app import db as web_db
from web.app.main import app
from web.app.services import accounts as account_service
from web.app.services import checkout as checkout_service
from web.app.services import wallet as wallet_service


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    checkout_service._config = None
    checkout_service._suppliers = None
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    yield database
    web_db.database = old
    checkout_service._config = None
    checkout_service._suppliers = None
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _stock_variant(s, price="100", codes=("AAA", "BBB", "CCC")) -> int:
    p = Product(game="G", title="Игра"); s.add(p); await s.flush()
    v = Variant(product_id=p.id, title="Ключ", liog_product_id=0, liog_variation_id=-1,
                source="stock", price_rub=Decimal(price))
    s.add(v); await s.flush()
    await stock_service.add_codes(s, v.id, list(codes))
    return v.id


async def _account_with_balance(s, email, balance="500"):
    acc = await account_service.register(s, email, "secret12")
    wid = await wallet_service.ensure_wallet(s, acc)
    w = await s.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
    w.balance = Decimal(balance)
    return acc


# ── web_buyable (какие типы доступны на этом этапе) ──────────────────────────
def test_web_buyable():
    mk = lambda **kw: Variant(product_id=1, title="t", liog_product_id=0,
                              liog_variation_id=-1, **kw)
    assert checkout_service.web_buyable(mk(source="stock")) is True
    assert checkout_service.web_buyable(mk(source="liogames")) is True
    assert checkout_service.web_buyable(mk(source="vpnresellers")) is True
    assert checkout_service.web_buyable(mk(source="fazercard", fzr_kind="gamekey")) is True
    assert checkout_service.web_buyable(mk(source="fazercard", fzr_kind="giftcard")) is True
    # Telegram Звёзды/Премиум — теперь тоже доступны:
    assert checkout_service.web_buyable(mk(source="fazercard", fzr_a="telegram_stars")) is True
    assert checkout_service.web_buyable(mk(source="fazercard", fzr_a="telegram_premium")) is True
    # ещё не на сайте:
    assert checkout_service.web_buyable(mk(source="fazercard", fzr_kind="steam")) is False
    assert checkout_service.web_buyable(mk(source="socproof")) is False


# ── Покупка из стока: мгновенная выдача кодов ────────────────────────────────
@pytest.mark.asyncio
async def test_buy_stock_instant(db):
    async with db.session() as s:
        vid = await _stock_variant(s)
        acc = await _account_with_balance(s, "a@a.com", "500")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        res = await checkout_service.purchase(s, acc, vid, 2)
        assert res["status"] == OrderStatus.COMPLETED.value
        assert res["kind"] == "codes" and len(res["codes"]) == 2
    # баланс списан: 500 − 2×100 = 300
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        bal = await wallet_service.get_balance(s, acc)
        assert bal == Decimal("300")


@pytest.mark.asyncio
async def test_buy_insufficient_balance(db):
    async with db.session() as s:
        vid = await _stock_variant(s)
        acc = await _account_with_balance(s, "b@b.com", "50")   # не хватит на 100
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="Недостаточно"):
            await checkout_service.purchase(s, acc, vid, 1)
    # баланс не тронут
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        assert await wallet_service.get_balance(s, acc) == Decimal("50")


@pytest.mark.asyncio
async def test_buy_out_of_stock(db):
    async with db.session() as s:
        vid = await _stock_variant(s, codes=("ONLY1",))     # всего 1 код
        acc = await _account_with_balance(s, "c@c.com", "500")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="наличии"):
            await checkout_service.purchase(s, acc, vid, 3)   # просим 3
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        assert await wallet_service.get_balance(s, acc) == Decimal("500")  # откат


@pytest.mark.asyncio
async def test_buy_unsupported_type_rejected(db):
    async with db.session() as s:
        p = Product(game="S", title="SMM"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="лайки", liog_product_id=0, liog_variation_id=-1,
                    source="socproof", smm_service=123, smm_rate=Decimal("10"))
        s.add(v); await s.flush()
        acc = await _account_with_balance(s, "d@d.com", "500")
        await s.commit(); acc_id, vid = acc.id, v.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="скоро"):
            await checkout_service.purchase(s, acc, vid, 1)


@pytest.mark.asyncio
async def test_buy_requires_auth(db):
    async with db.session() as s:
        vid = await _stock_variant(s)
        await s.commit()
    async with _client() as c:
        r = await c.post("/api/buy", json={"variant_id": vid, "quantity": 1})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_buy_endpoint_end_to_end(db):
    async with db.session() as s:
        vid = await _stock_variant(s)
        await s.commit()
    async with _client() as c:
        reg = await c.post("/api/auth/register", json={"email": "e@e.com", "password": "secret12"})
        token = reg.json()["token"]
        # пополним баланс вручную (как бы после оплаты)
        async with db.session() as s:
            acc = await account_service.get_by_email(s, "e@e.com")
            wid = await wallet_service.ensure_wallet(s, acc)
            w = await s.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
            w.balance = Decimal("500"); await s.commit()
        r = await c.post("/api/buy", json={"variant_id": vid, "quantity": 1},
                         headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == OrderStatus.COMPLETED.value and data["codes"]
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["balance_rub"] == 400.0

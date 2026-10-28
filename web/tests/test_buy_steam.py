"""Покупка «Пополнение Steam» на сайте (FazerCard: логин + валюта + сумма)."""

from decimal import Decimal

import pytest

from bot.db.base import Database
from bot.db.models import HEAD_MERCHANT_ID, OrderStatus, Product, User, Variant
from bot.services import steam as steam_service
from web.app import db as web_db
from web.app.services import accounts as account_service
from web.app.services import checkout as checkout_service
from web.app.services import orders_view as ov
from web.app.services import wallet as wallet_service


class _FakeFzr:
    def steam_rates(self):
        return {"rates": {"RUB": "90", "KZT": "500", "UAH": "40"}}

    def steam_check_login(self, login):
        return {"valid": login != "bad"}

    def order_steam_topup(self, *, steam_login, amount, currency, idempotency_key):
        return {"order": {"id": "st-1", "status": "processing"}}


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    steam_service._rates_cache = {}
    steam_service._rates_ts = 0.0
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    checkout_service._sup()
    checkout_service._suppliers["fazercard"] = _FakeFzr()
    yield database
    web_db.database = old
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    await database.dispose()


async def _steam_variant(s) -> int:
    p = Product(game="St", title="Steam"); s.add(p); await s.flush()
    v = Variant(product_id=p.id, title="Пополнение Steam", liog_product_id=0,
                liog_variation_id=-1, source="fazercard", fzr_kind="steam",
                fzr_a="steam_topup", price_rub=Decimal("0"))
    s.add(v); await s.flush()
    return v.id


async def _account(s, email="a@a.com", balance="5000"):
    acc = await account_service.register(s, email, "secret12")
    wid = await wallet_service.ensure_wallet(s, acc)
    w = await s.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
    w.balance = Decimal(balance)
    return acc


def test_buy_kind_steam():
    v = Variant(product_id=1, title="t", liog_product_id=0, liog_variation_id=-1,
                source="fazercard", fzr_kind="steam", fzr_a="steam_topup")
    assert checkout_service.buy_kind(v) == "steam"


@pytest.mark.asyncio
async def test_steam_config_and_quote(db):
    async with db.session() as s:
        cfg = await checkout_service.steam_config(s)
        codes = {c["code"] for c in cfg["currencies"]}
        assert {"RUB", "USD", "KZT", "UAH"} <= codes
        q = await checkout_service.steam_quote(s, Decimal("500"), "RUB")
        assert q["price_rub"] and q["price_rub"] > 0
        # заведомо вне лимитов (слишком много)
        assert (await checkout_service.steam_quote(s, Decimal("9999999"), "RUB"))["price_rub"] is None


@pytest.mark.asyncio
async def test_buy_steam(db):
    async with db.session() as s:
        vid = await _steam_variant(s)
        acc = await _account(s, "st@st.com", "5000")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        price = (await checkout_service.steam_quote(s, Decimal("500"), "RUB"))["price_rub"]
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        res = await checkout_service.purchase(
            s, acc, vid, 1, {"steam_login": "mylogin", "currency": "RUB", "amount": "500"}
        )
        assert res["status"] == OrderStatus.FULFILLING.value
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        bal = await wallet_service.get_balance(s, acc)
        assert bal == Decimal("5000") - Decimal(str(price))
        det = await ov.order_detail(s, acc, res["order_id"])
        labels = {f["label"]: f["value"] for f in det["info"]}
        assert labels["Steam-логин"] == "mylogin"
        assert "500" in labels["Сумма"] and "RUB" in labels["Сумма"]


@pytest.mark.asyncio
async def test_buy_steam_bad_login(db):
    async with db.session() as s:
        vid = await _steam_variant(s)
        acc = await _account(s, "b@b.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="логин"):
            await checkout_service.purchase(
                s, acc, vid, 1, {"steam_login": "bad", "currency": "RUB", "amount": "500"}
            )


@pytest.mark.asyncio
async def test_buy_steam_no_login(db):
    async with db.session() as s:
        vid = await _steam_variant(s)
        acc = await _account(s, "c@c.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="логин"):
            await checkout_service.purchase(
                s, acc, vid, 1, {"currency": "RUB", "amount": "500"}
            )

"""Покупка Telegram Звёзд и Премиума на сайте (FazerCard, спец-эндпоинты)."""

from decimal import Decimal

import pytest

from bot.db.base import Database
from bot.db.models import HEAD_MERCHANT_ID, OrderStatus, Product, User, Variant
from bot.services import stars as stars_service
from web.app import db as web_db
from web.app.services import accounts as account_service
from web.app.services import checkout as checkout_service
from web.app.services import orders_view as ov
from web.app.services import wallet as wallet_service


class _FakeFzr:
    """Заглушка FazerCard: котировка звёзд + спец-эндпоинты (без сети)."""

    def telegram_stars_quote(self):
        return {"price_per_star": "0.015", "min_amount": 50, "max_amount": 10000}

    def order_telegram_stars(self, *, telegram_username, quantity, idempotency_key):
        return {"order": {"id": "fzr-st-1", "status": "processing"}}

    def order_telegram_premium(self, *, telegram_username, months, idempotency_key):
        return {"order": {"id": "fzr-pr-1", "status": "processing"}}


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    stars_service._quote_cache = None
    stars_service._quote_ts = 0.0
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    checkout_service._sup()                       # заполнить кэш поставщиков
    checkout_service._suppliers["fazercard"] = _FakeFzr()   # внедрить заглушку
    yield database
    web_db.database = old
    checkout_service._config = None
    checkout_service._suppliers = None
    ov._config = None
    await database.dispose()


async def _tg_variant(s, fzr_a, title) -> int:
    p = Product(game="TG", title="Telegram"); s.add(p); await s.flush()
    v = Variant(product_id=p.id, title=title, liog_product_id=0, liog_variation_id=-1,
                source="fazercard", fzr_kind="topup", fzr_a=fzr_a, fzr_b="",
                price_rub=Decimal("299"))
    s.add(v); await s.flush()
    return v.id


async def _account(s, email="a@a.com", balance="5000"):
    acc = await account_service.register(s, email, "secret12")
    wid = await wallet_service.ensure_wallet(s, acc)
    w = await s.get(User, {"id": wid, "merchant_id": HEAD_MERCHANT_ID})
    w.balance = Decimal(balance)
    return acc


# ── buy_kind ─────────────────────────────────────────────────────────────────
def test_buy_kind_tg():
    mk = lambda **kw: Variant(product_id=1, title="t", liog_product_id=0,
                              liog_variation_id=-1, source="fazercard", **kw)
    assert checkout_service.buy_kind(mk(fzr_a="telegram_stars")) == "stars"
    assert checkout_service.buy_kind(mk(fzr_a="telegram_premium")) == "premium"


# ── Котировка звёзд ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stars_quote(db):
    async with db.session() as s:
        q = await checkout_service.stars_quote(s)
        assert q["min"] == 50 and q["max"] == 10000 and q["price_rub"] is None
        q100 = await checkout_service.stars_quote(s, 100)
        # 100 × 0.015 × 95 × 1.10 ≈ 156.75 → округление вверх
        assert q100["price_rub"] and q100["price_rub"] >= 150
        # вне диапазона — цены нет
        assert (await checkout_service.stars_quote(s, 10))["price_rub"] is None


# ── Покупка звёзд ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_buy_stars(db):
    async with db.session() as s:
        vid = await _tg_variant(s, "telegram_stars", "Telegram Звёзды")
        acc = await _account(s, "st@st.com", "5000")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        price = (await checkout_service.stars_quote(s, 500))["price_rub"]
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        res = await checkout_service.purchase(s, acc, vid, 500, {"telegram_username": "durov"})
        assert res["status"] == OrderStatus.FULFILLING.value
        assert res["kind"] == "pending"
    # списано по рассчитанной цене; заказ показывает получателя
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        bal = await wallet_service.get_balance(s, acc)
        assert bal == Decimal("5000") - Decimal(str(price))
        det = await ov.order_detail(s, acc, res["order_id"])
        labels = {f["label"]: f["value"] for f in det["info"]}
        assert labels["Получатель"] == "@durov"
        assert "500" in labels["Количество"]


@pytest.mark.asyncio
async def test_buy_stars_bad_username(db):
    async with db.session() as s:
        vid = await _tg_variant(s, "telegram_stars", "Звёзды")
        acc = await _account(s, "b@b.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="username"):
            await checkout_service.purchase(s, acc, vid, 100, {"telegram_username": "@@"})


@pytest.mark.asyncio
async def test_buy_stars_qty_out_of_range(db):
    async with db.session() as s:
        vid = await _tg_variant(s, "telegram_stars", "Звёзды")
        acc = await _account(s, "c@c.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="звёзд"):
            await checkout_service.purchase(s, acc, vid, 10, {"telegram_username": "durov"})


# ── Покупка премиума ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_buy_premium(db):
    async with db.session() as s:
        vid = await _tg_variant(s, "telegram_premium", "Telegram Premium 3 месяца")
        acc = await _account(s, "p@p.com", "1000")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        res = await checkout_service.purchase(s, acc, vid, 1, {"telegram_username": "durov"})
        assert res["status"] == OrderStatus.FULFILLING.value
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        bal = await wallet_service.get_balance(s, acc)
        assert bal == Decimal("1000") - Decimal("299")      # фикс-цена номинала
        det = await ov.order_detail(s, acc, res["order_id"])
        assert det["info"][0]["value"] == "@durov"


@pytest.mark.asyncio
async def test_buy_premium_requires_username(db):
    async with db.session() as s:
        vid = await _tg_variant(s, "telegram_premium", "Premium 1 месяц")
        acc = await _account(s, "d@d.com")
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(checkout_service.CheckoutError, match="username"):
            await checkout_service.purchase(s, acc, vid, 1, {})

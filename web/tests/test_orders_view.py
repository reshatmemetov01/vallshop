"""История заказов аккаунта на сайте: список, детали, выдача, изоляция."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import (
    HEAD_MERCHANT_ID,
    Order,
    OrderStatus,
    Product,
    User,
    Variant,
)
from web.app import db as web_db
from web.app.main import app
from web.app.services import accounts as account_service
from web.app.services import orders_view as ov
from web.app.services import wallet as wallet_service


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    ov._config = None
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    yield database
    web_db.database = old
    ov._config = None
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _variant(s, source="stock", title="Ключ") -> int:
    p = Product(game="G", title="Игра"); s.add(p); await s.flush()
    v = Variant(product_id=p.id, title=title, liog_product_id=0, liog_variation_id=-1,
                source=source, price_rub=Decimal("100"))
    s.add(v); await s.flush()
    return v.id


async def _account(s, email="a@a.com"):
    acc = await account_service.register(s, email, "secret12")
    await wallet_service.ensure_wallet(s, acc)
    return acc


@pytest.mark.asyncio
async def test_list_and_detail_stock(db):
    async with db.session() as s:
        vid = await _variant(s)
        acc = await _account(s)
        wid = acc.wallet_user_id
        s.add(Order(client_ref="o1", user_id=wid, merchant_id=0, variant_id=vid,
                    price_usd=Decimal("100"), quantity=2, status=OrderStatus.COMPLETED,
                    delivery_code="CODE-1\nCODE-2"))
        await s.commit(); acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        lst = await ov.list_orders(s, acc)
        assert len(lst) == 1
        assert lst[0]["title"] == "Ключ" and lst[0]["total_rub"] == 200.0
        assert lst[0]["status"] == OrderStatus.COMPLETED.value
        assert lst[0]["status_label"] == "выполнен"
        det = await ov.order_detail(s, acc, lst[0]["id"])
        assert det["codes"] == ["CODE-1", "CODE-2"]


@pytest.mark.asyncio
async def test_detail_smm_progress(db):
    async with db.session() as s:
        vid = await _variant(s, source="socproof", title="Лайки")
        acc = await _account(s, "b@b.com")
        wid = acc.wallet_user_id
        o = Order(client_ref="o2", user_id=wid, merchant_id=0, variant_id=vid,
                  price_usd=Decimal("100"), quantity=1, status=OrderStatus.FULFILLING,
                  supplier="socproof",
                  fields_json='{"link":"https://t.me/x","quantity":1000,"remains":400,"progress":60,"status":"In progress"}')
        s.add(o); await s.commit(); acc_id, oid = acc.id, None
        await s.refresh(o); oid = o.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        det = await ov.order_detail(s, acc, oid)
        assert det["smm"]["progress"] == 60
        assert det["smm"]["done"] == 600 and det["smm"]["quantity"] == 1000
        assert det["smm"]["link"] == "https://t.me/x"


@pytest.mark.asyncio
async def test_cannot_see_other_accounts_order(db):
    async with db.session() as s:
        vid = await _variant(s)
        a1 = await _account(s, "x@x.com")
        a2 = await _account(s, "y@y.com")
        s.add(Order(client_ref="o3", user_id=a1.wallet_user_id, merchant_id=0,
                    variant_id=vid, price_usd=Decimal("100"), quantity=1,
                    status=OrderStatus.COMPLETED, delivery_code="SECRET"))
        await s.commit(); a2_id = a2.id
        o_id = (await s.scalar(Order.__table__.select().with_only_columns(Order.id)))
    async with db.session() as s:
        a2 = await account_service.get_by_id(s, a2_id)
        assert await ov.order_detail(s, a2, o_id) is None     # чужой заказ не виден
        assert await ov.list_orders(s, a2) == []              # и не в списке


@pytest.mark.asyncio
async def test_orders_endpoint_requires_auth(db):
    async with _client() as c:
        assert (await c.get("/api/orders")).status_code == 401

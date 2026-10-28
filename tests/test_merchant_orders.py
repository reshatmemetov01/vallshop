"""Раздел «Заказы» в кабинете франшизы: статусы, данные, покупатель, изоляция."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from bot.db import Database
from bot.db.models import Order, OrderStatus, User
from bot.handlers.merchant_admin import (
    _ord_buyer, _ord_emoji, _ord_fields, _ord_status,
)


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


def test_status_and_emoji():
    assert _ord_status(OrderStatus.COMPLETED) == "✅ выполнен"
    assert _ord_status(OrderStatus.FULFILLING) == "⏳ выдаётся"
    assert _ord_emoji(OrderStatus.COMPLETED) == "✅"
    assert _ord_emoji(OrderStatus.REFUNDED) == "↩️"
    assert _ord_emoji(OrderStatus.NEEDS_ACTION) == "⚠️"


def test_fields_stars_and_smm():
    stars = Order(client_ref="s", user_id=1, merchant_id=5, variant_id=1,
                  price_usd=Decimal("100"), quantity=1, status=OrderStatus.FULFILLING,
                  fields_json='[{"label":"Получатель","value":"@durov"},{"label":"Количество","value":"500 ⭐️"}]')
    out = _ord_fields(stars)
    assert "Получатель" in out and "@durov" in out

    smm = Order(client_ref="m", user_id=1, merchant_id=5, variant_id=1,
                price_usd=Decimal("100"), quantity=1, status=OrderStatus.FULFILLING,
                fields_json='{"link":"https://t.me/x","quantity":1000,"remains":400,"progress":60}')
    out2 = _ord_fields(smm)
    assert "60%" in out2 and "600 из 1000" in out2 and "https://t.me/x" in out2


@pytest.mark.asyncio
async def test_buyer_label(db):
    async with db.session() as s:
        s.add(User(id=42, merchant_id=5, username="client1"))
        s.add(User(id=43, merchant_id=5, full_name="Иван"))
        await s.commit()
    async with db.session() as s:
        o1 = Order(client_ref="a", user_id=42, merchant_id=5, variant_id=1,
                   price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED)
        o2 = Order(client_ref="b", user_id=43, merchant_id=5, variant_id=1,
                   price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED)
        o3 = Order(client_ref="c", user_id=99, merchant_id=5, variant_id=1,
                   price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED)
        assert await _ord_buyer(s, o1) == "@client1"
        assert await _ord_buyer(s, o2) == "Иван"
        assert await _ord_buyer(s, o3) == "id 99"   # нет записи — показываем id


@pytest.mark.asyncio
async def test_orders_scoped_by_merchant(db):
    async with db.session() as s:
        s.add(Order(client_ref="h", user_id=1, merchant_id=0, variant_id=1,
                    price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED))
        s.add(Order(client_ref="f1", user_id=2, merchant_id=5, variant_id=1,
                    price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED))
        s.add(Order(client_ref="f2", user_id=3, merchant_id=5, variant_id=1,
                    price_usd=Decimal("1"), quantity=1, status=OrderStatus.COMPLETED))
        await s.commit()
    async with db.session() as s:
        fr = list(await s.scalars(select(Order).where(Order.merchant_id == 5)))
        assert len(fr) == 2
        head = list(await s.scalars(select(Order).where(Order.merchant_id == 0)))
        assert len(head) == 1

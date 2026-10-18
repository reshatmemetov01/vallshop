"""Статистика: продажи, выручка, себестоимость, прибыль, пользователи."""

from decimal import Decimal

import pytest

from bot.db import Database, Product, Variant
from bot.db.models import Order, OrderStatus, User
from bot.services import stats as stats_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_sales_and_profit(db):
    async with db.session() as s:
        p = Product(game="X", title="T")
        s.add(p)
        await s.flush()
        v = Variant(
            product_id=p.id, title="v", liog_product_id=1, liog_variation_id=1,
            cost_usd=Decimal("2.00"),
        )
        s.add(v)
        await s.flush()
        s.add(User(id=1))
        # завершённый заказ: 2 шт по 5.00, себестоимость 2.00
        s.add(Order(client_ref="a", user_id=1, variant_id=v.id,
                    price_usd=Decimal("5.00"), quantity=2, status=OrderStatus.COMPLETED))
        # не завершённый — не считается
        s.add(Order(client_ref="b", user_id=1, variant_id=v.id,
                    price_usd=Decimal("5.00"), quantity=1, status=OrderStatus.AWAITING_PAYMENT))
        await s.commit()

    async with db.session() as s:
        st = await stats_service.sales_stats(s, None, __import__("datetime").datetime.utcnow().replace(year=2100))
        assert st["orders"] == 1
        assert st["items"] == 2
        assert st["revenue"] == Decimal("10.00")   # 5*2
        assert st["cost"] == Decimal("380.00")      # 2*2 USDT × курс 95
        assert st["cost_usd"] == Decimal("4.00")
        assert st["profit"] == Decimal("-370.00")
        us = await stats_service.users_stats(s, None, __import__("datetime").datetime.utcnow().replace(year=2100))
        assert us["total"] == 1 and us["new"] == 1


@pytest.mark.asyncio
async def test_new_user_has_created_at_and_counts(db):
    # Регрессия: у нового пользователя проставляется created_at (Python-дефолт),
    # иначе «Новых за период» всегда 0 (created_at = NULL после перестройки users).
    from datetime import datetime, timedelta
    from bot.services import orders as order_service
    async with db.session() as s:
        u = await order_service.ensure_user(s, 10, "u10", "Ten", 0)
        await s.commit()
        assert u.created_at is not None
    end = datetime.utcnow() + timedelta(days=1)
    start = datetime.utcnow() - timedelta(days=1)
    async with db.session() as s:
        us = await stats_service.users_stats(s, start, end)
        assert us["total"] == 1 and us["new"] == 1   # новый юзер попал в период


@pytest.mark.asyncio
async def test_total_balance(db):
    async with db.session() as s:
        s.add(User(id=1, balance=Decimal("290.00")))
        s.add(User(id=2, balance=Decimal("10.50")))
        s.add(User(id=3))  # баланс 0 по умолчанию
        await s.commit()
    async with db.session() as s:
        assert await stats_service.total_balance(s) == Decimal("300.50")


def test_preset_labels():
    _, _, lbl = stats_service.preset_range("today")
    assert lbl == "Сегодня"
    s, _, lbl2 = stats_service.preset_range("all")
    assert s is None and lbl2 == "Всё время"


def test_day_bounds_local_timezone_offset():
    # «Сегодня»/день считаются по местному поясу: граница суток смещена от UTC.
    from datetime import datetime
    d = datetime(2026, 10, 18)
    start_msk, end_msk = stats_service.day_bounds(d, "Europe/Moscow")
    # МСК = UTC+3, поэтому сутки начинаются в 21:00 UTC предыдущего дня
    assert start_msk == datetime(2026, 10, 17, 21, 0, 0)
    assert end_msk == datetime(2026, 10, 18, 21, 0, 0)
    # UTC-пояс — ровно полночь
    start_utc, _ = stats_service.day_bounds(d, "UTC")
    assert start_utc == datetime(2026, 10, 18, 0, 0, 0)


@pytest.mark.asyncio
async def test_order_near_midnight_counts_in_local_today(db):
    # Регрессия: заказ в 01:00 по МСК (= 22:00 UTC вчера) попадает в «сегодня» по МСК.
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy import text
    msk = ZoneInfo("Europe/Moscow")
    now_msk = datetime.now(msk)
    local_0100 = now_msk.replace(hour=1, minute=0, second=0, microsecond=0)
    utc_naive = local_0100.astimezone(timezone.utc).replace(tzinfo=None)
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="v", liog_product_id=1, liog_variation_id=1,
                    cost_usd=Decimal("1")); s.add(v); await s.flush()
        s.add(User(id=1))
        o = Order(client_ref="a", user_id=1, variant_id=v.id, price_usd=Decimal("100"),
                  quantity=1, status=OrderStatus.COMPLETED)
        s.add(o); await s.flush()
        await s.execute(text("UPDATE orders SET created_at=:c WHERE id=:i"),
                        {"c": utc_naive.strftime("%Y-%m-%d %H:%M:%S"), "i": o.id})
        await s.commit()
    start, end, _ = stats_service.preset_range("today", "Europe/Moscow")
    async with db.session() as s:
        assert (await stats_service.sales_stats(s, start, end))["orders"] == 1

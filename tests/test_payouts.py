"""Выплаты франшизам: кошельки, заявки (резерв), одобрение/отклонение (возврат)."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant, MerchantPayout
from bot.services import payouts as pay


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _merchant(db, earnings="1000"):
    async with db.session() as s:
        m = Merchant(title="A", bot_token="t:x", bot_username="a", admin_user_id=1,
                     earnings_rub=Decimal(earnings), earnings_total_rub=Decimal(earnings))
        s.add(m); await s.commit()
        return m.id


@pytest.mark.asyncio
async def test_wallets_bind(db):
    mid = await _merchant(db)
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+79001234567", "Сбер")
        await pay.set_usdt(s, mid, "0xABCDEF0123456789")
        await s.commit()
        w = await pay.wallets(s, mid)
    assert w["sbp"] == {"phone": "+79001234567", "bank": "Сбер"}
    assert w["usdt"] == "0xABCDEF0123456789"
    assert pay.wallet_label("sbp", w) == "СБП +79001234567 (Сбер)"
    assert pay.wallet_label("usdt", w) == "USDT BEP20 0xABCDEF0123456789"


@pytest.mark.asyncio
async def test_create_request_validations(db):
    mid = await _merchant(db, earnings="400")
    async with db.session() as s:
        # нет кошелька
        p, err = await pay.create_request(s, mid, Decimal("500"), "sbp")
        assert p is None and "привяжите" in err.lower()
        await pay.set_sbp(s, mid, "+7900", "Сбер"); await s.commit()
    async with db.session() as s:
        # ниже минимума
        p, err = await pay.create_request(s, mid, Decimal("100"), "sbp")
        assert p is None and "минимал" in err.lower()
        # больше баланса (400)
        p, err = await pay.create_request(s, mid, Decimal("500"), "sbp")
        assert p is None and "недостаточно" in err.lower()


@pytest.mark.asyncio
async def test_request_reserves_and_approve(db):
    mid = await _merchant(db, earnings="1000")
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+7900", "Сбер")
        p, err = await pay.create_request(s, mid, Decimal("600"), "sbp")
        await s.commit()
        assert err is None and p.status == "pending"
        pid = p.id
        # сумма зарезервирована (списана с доступного)
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("400")
        # total не изменился
        assert (await s.get(Merchant, mid)).earnings_total_rub == Decimal("1000")
    async with db.session() as s:
        done = await pay.approve(s, pid)
        await s.commit()
        assert done.status == "approved"
        # баланс не меняется при одобрении (уже зарезервировано)
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("400")
        # повторное одобрение — нет
        assert await pay.approve(s, pid) is None


@pytest.mark.asyncio
async def test_reject_refunds(db):
    mid = await _merchant(db, earnings="1000")
    async with db.session() as s:
        await pay.set_usdt(s, mid, "0x1234567890")
        p, _ = await pay.create_request(s, mid, Decimal("700"), "usdt")
        await s.commit()
        pid = p.id
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("300")
    async with db.session() as s:
        r = await pay.reject(s, pid, "неверные реквизиты")
        await s.commit()
        assert r.status == "rejected" and r.reason == "неверные реквизиты"
        # сумма вернулась на баланс
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("1000")

"""Пополнение баланса на сайте: способы, создание счёта, статус, зачисление."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import HEAD_MERCHANT_ID, TopUp, TopUpStatus, User
from bot.services import balance as balance_service
from web.app import db as web_db
from web.app.main import app
from web.app.services import accounts as account_service
from web.app.services import payments as payments_service
from web.app.services import wallet as wallet_service


@pytest.fixture
async def db(monkeypatch):
    # mock-режим платёжек: провайдеры считаются настроенными, без сети.
    monkeypatch.setenv("BOT_TOKEN", "123456:TESTTOKEN")
    monkeypatch.setenv("PAYHOT_MOCK", "1")
    monkeypatch.setenv("PAYDEX_MOCK", "1")
    payments_service._config = None       # сброс кэша конфига/провайдеров
    payments_service._providers = None

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    yield database
    web_db.database = old
    payments_service._config = None
    payments_service._providers = None
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(c) -> str:
    r = await c.post("/api/auth/register", json={"email": "u@u.com", "password": "secret12"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.mark.asyncio
async def test_methods_available(db):
    async with db.session() as s:
        methods = await payments_service.available_methods(s)
    ids = {m["id"] for m in methods}
    assert "card" in ids          # PayHot (mock) включён по умолчанию
    assert "sbp" in ids           # Paydex (mock)
    assert "sbp_ph" not in ids    # резерв скрыт по умолчанию


@pytest.mark.asyncio
async def test_create_topup_returns_checkout(db):
    async with _client() as c:
        token = await _register(c)
        r = await c.post("/api/topup", json={"amount": "500", "method": "card"},
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["checkout_url"] and data["status"] == "pending"
        tid = data["topup_id"]
        # статус — pending, принадлежит аккаунту
        rs = await c.get(f"/api/topup/{tid}", headers={"Authorization": f"Bearer {token}"})
        assert rs.status_code == 200
        assert rs.json()["status"] == TopUpStatus.PENDING


@pytest.mark.asyncio
async def test_topup_below_min_rejected(db):
    async with _client() as c:
        token = await _register(c)
        r = await c.post("/api/topup", json={"amount": "10", "method": "card"},
                         headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
        assert "от" in r.json()["detail"]


@pytest.mark.asyncio
async def test_topup_requires_auth(db):
    async with _client() as c:
        r = await c.post("/api/topup", json={"amount": "500", "method": "card"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_settlement_credits_account_balance(db):
    """Эмуляция того, что делает поллер бота: PAID → credit. Баланс виден на сайте."""
    async with _client() as c:
        token = await _register(c)
        await c.post("/api/topup", json={"amount": "500", "method": "card"},
                     headers={"Authorization": f"Bearer {token}"})
    # «оплачиваем» (как бот): помечаем PAID и зачисляем
    async with db.session() as s:
        t = await s.scalar(TopUp.__table__.select().limit(1).with_only_columns(TopUp.id))
        topup = await s.get(TopUp, t)
        topup.status = TopUpStatus.PAID
        await balance_service.credit(s, topup.user_id, topup.amount_usd)
        await s.commit()
    async with _client() as c:
        token_me = await c.post("/api/auth/login", json={"email": "u@u.com", "password": "secret12"})
        token = token_me.json()["token"]
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["balance_rub"] == 500.0


class _FakeBolt:
    """Заглушка BoltUtil без сети: возвращает hosted checkout + крипто-поля."""

    name = "boltutil"

    async def create_invoice(self, *, amount, client_ref, description,
                             notify_url=None, success_url=None, network=None):
        from bot.payments.base import Invoice
        return Invoice(
            provider_order_id="bolt-" + client_ref,
            checkout_url="https://pay.bolt/" + client_ref,
            address="TAddrXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            network=network or "TRC20",
            amount=Decimal(amount),
            currency="USDT",
        )


@pytest.mark.asyncio
async def test_usdt_topup_converts_and_stores_crypto(db):
    payments_service._provs()                 # заполнить кэш провайдеров
    payments_service._providers["bolt"] = _FakeBolt()   # внедрить заглушку
    async with db.session() as s:
        acc = await account_service.register(s, "c@c.com", "secret12")
        await s.commit()
        acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        topup = await payments_service.create_topup(s, acc, Decimal("500"), "usdt")
        await s.commit()
        assert topup.provider == "boltutil"
        assert topup.currency == "USDT"
        assert topup.address and topup.checkout_url
        # 500 ₽ / курс 95 ≈ 5.26 USDT
        assert topup.pay_amount == Decimal("5.26")
        assert topup.amount_usd == Decimal("500.00")   # зачислим рубли


@pytest.mark.asyncio
async def test_usdt_below_min_rejected(db):
    payments_service._provs()
    payments_service._providers["bolt"] = _FakeBolt()
    async with db.session() as s:
        acc = await account_service.register(s, "d@d.com", "secret12")
        await s.commit()
        acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        # 50 ₽ / 95 ≈ 0.53 USDT < 1 USDT (минимум BoltUtil)
        with pytest.raises(payments_service.TopupError, match="минимум"):
            await payments_service.create_topup(s, acc, Decimal("50"), "usdt")


@pytest.mark.asyncio
async def test_usdt_hidden_when_bolt_not_configured(db):
    # в тестовом окружении BoltUtil не настроен → usdt не показываем на сайте
    async with db.session() as s:
        methods = await payments_service.available_methods(s)
    assert "usdt" not in {m["id"] for m in methods}


@pytest.mark.asyncio
async def test_cannot_see_other_accounts_topup(db):
    async with _client() as c:
        t1 = await _register(c)
        r = await c.post("/api/topup", json={"amount": "500", "method": "card"},
                         headers={"Authorization": f"Bearer {t1}"})
        tid = r.json()["topup_id"]
        # другой аккаунт
        r2 = await c.post("/api/auth/register", json={"email": "b@b.com", "password": "secret12"})
        t2 = r2.json()["token"]
        rs = await c.get(f"/api/topup/{tid}", headers={"Authorization": f"Bearer {t2}"})
        assert rs.status_code == 404

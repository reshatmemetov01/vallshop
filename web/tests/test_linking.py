"""Кошелёк, проверка Telegram Login Widget, вход/привязка и слияние кошельков."""

import hashlib
import hmac
import time
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import HEAD_MERCHANT_ID, Order, OrderStatus, User, WebAccount
from web.app import db as web_db
from web.app.config import settings
from web.app.main import app
from web.app.security import hash_password
from web.app.services import accounts as account_service
from web.app.services import linking as linking_service
from web.app.services import wallet as wallet_service
from web.app.telegram import verify_login_widget

TEST_TOKEN = "123456:TESTTOKEN"


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    old_token = settings.telegram_bot_token
    settings.telegram_bot_token = TEST_TOKEN
    yield database
    web_db.database = old
    settings.telegram_bot_token = old_token
    await database.dispose()


def _sign(data: dict, token: str = TEST_TOKEN) -> dict:
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    secret = hashlib.sha256(token.encode()).digest()
    data = dict(data)
    data["hash"] = hmac.new(secret, "\n".join(pairs).encode(), hashlib.sha256).hexdigest()
    return data


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Миграция старой схемы web_accounts (email NOT NULL → nullable) ────────────
@pytest.mark.asyncio
async def test_migrate_web_accounts_nullable():
    from sqlalchemy.ext.asyncio import create_async_engine

    from web.app.migrate import ensure_web_accounts_nullable

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    old = (
        "CREATE TABLE web_accounts (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
        "email VARCHAR(255) NOT NULL, password_hash VARCHAR(255) NOT NULL, "
        "email_verified BOOLEAN DEFAULT '0' NOT NULL, telegram_user_id BIGINT, "
        "wallet_user_id BIGINT, is_blocked BOOLEAN DEFAULT '0' NOT NULL, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, last_login_at DATETIME)"
    )
    async with eng.begin() as c:
        await c.exec_driver_sql(old)
        await c.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_web_accounts_email ON web_accounts(email)"
        )
        await c.exec_driver_sql(
            "INSERT INTO web_accounts (email,password_hash) VALUES ('a@b.com','x')"
        )
    async with eng.begin() as c:
        await c.run_sync(ensure_web_accounts_nullable)
    async with eng.begin() as c:
        info = (await c.exec_driver_sql("PRAGMA table_info(web_accounts)")).fetchall()
        nn = {r[1]: r[3] for r in info}
        assert nn["email"] == 0 and nn["password_hash"] == 0        # стали nullable
        row = (await c.exec_driver_sql("SELECT email FROM web_accounts")).fetchone()
        assert row[0] == "a@b.com"                                  # запись сохранилась
        # Telegram-аккаунты без email (несколько NULL) — разрешены
        for tg in (6344910452, 999):
            await c.exec_driver_sql(
                "INSERT INTO web_accounts (email,password_hash,telegram_user_id,"
                f"wallet_user_id) VALUES (NULL,NULL,{tg},{tg})"
            )
        cnt = (await c.exec_driver_sql("SELECT COUNT(*) FROM web_accounts")).fetchone()[0]
        assert cnt == 3
    async with eng.begin() as c:
        await c.run_sync(ensure_web_accounts_nullable)              # идемпотентность
    await eng.dispose()


# ── Проверка подписи Telegram ────────────────────────────────────────────────
def test_verify_login_widget():
    data = _sign({"id": 42, "first_name": "A", "auth_date": int(time.time())})
    assert verify_login_widget(data, TEST_TOKEN) is True
    # подделка
    bad = dict(data); bad["id"] = 43
    assert verify_login_widget(bad, TEST_TOKEN) is False
    # чужой токен
    assert verify_login_widget(data, "other") is False
    # протухшее
    old = _sign({"id": 42, "auth_date": int(time.time()) - 999999})
    assert verify_login_widget(old, TEST_TOKEN) is False


# ── Слияние кошельков ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_merge_wallets(db):
    async with db.session() as s:
        s.add(User(id=-5, merchant_id=0, balance=Decimal("100")))   # синтетический
        s.add(User(id=777, merchant_id=0, balance=Decimal("50")))   # Telegram
        s.add(Order(client_ref="r1", user_id=-5, merchant_id=0, variant_id=1,
                    price_usd=Decimal("10"), quantity=1, status=OrderStatus.COMPLETED))
        await s.commit()
    async with db.session() as s:
        await linking_service.merge_wallets(s, -5, 777)
        await s.commit()
    async with db.session() as s:
        tg = await s.get(User, {"id": 777, "merchant_id": 0})
        assert tg.balance == Decimal("150")                 # 100 + 50
        assert await s.get(User, {"id": -5, "merchant_id": 0}) is None  # старый удалён
        o = await s.get(Order, 1)
        assert o.user_id == 777                             # заказ перенесён


# ── Вход через Telegram / привязка ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_telegram_login_creates_account(db):
    async with db.session() as s:
        acc = await linking_service.login_or_link_telegram(s, 777, None)
        assert acc.telegram_user_id == 777 and acc.wallet_user_id == 777
    # повторный вход — тот же аккаунт
    async with db.session() as s:
        acc2 = await linking_service.login_or_link_telegram(s, 777, None)
        assert acc2.id == acc.id


@pytest.mark.asyncio
async def test_link_sums_balances(db):
    # email-аккаунт с кошельком, где лежит 30; Telegram-кошелёк с 70 (из бота)
    async with db.session() as s:
        acc = await account_service.register(s, "u@u.com", "secret12")
        wid = await wallet_service.ensure_wallet(s, acc)       # синтетический
        w = await s.get(User, {"id": wid, "merchant_id": 0})
        w.balance = Decimal("30")
        s.add(User(id=777, merchant_id=0, balance=Decimal("70")))
        await s.commit()
        acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        linked = await linking_service.login_or_link_telegram(s, 777, acc)
        assert linked.telegram_user_id == 777 and linked.wallet_user_id == 777
    async with db.session() as s:
        tg = await s.get(User, {"id": 777, "merchant_id": 0})
        assert tg.balance == Decimal("100")                    # 30 + 70 сложены


@pytest.mark.asyncio
async def test_link_conflict(db):
    async with db.session() as s:
        other = WebAccount(telegram_user_id=777, wallet_user_id=777)
        s.add(other)
        acc = await account_service.register(s, "u2@u.com", "secret12")
        await s.commit()
        acc_id = acc.id
    async with db.session() as s:
        acc = await account_service.get_by_id(s, acc_id)
        with pytest.raises(linking_service.LinkError):
            await linking_service.login_or_link_telegram(s, 777, acc)


# ── Эндпоинт /api/auth/telegram ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_telegram_endpoint(db):
    payload = _sign({"id": 999, "first_name": "Tg", "auth_date": int(time.time())})
    async with _client() as c:
        r = await c.post("/api/auth/telegram", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["account"]["telegram_linked"] is True
        token = data["token"]
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and "balance_rub" in me.json()
    # битая подпись — 401
    async with _client() as c:
        bad = dict(payload); bad["id"] = 1000
        assert (await c.post("/api/auth/telegram", json=bad)).status_code == 401

"""Подтверждение email и сброс пароля (токены в БД; SMTP не требуется в тестах)."""

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from bot.db.base import Database
from bot.db.models import EmailToken, WebAccount
from web.app import db as web_db
from web.app.main import app
from web.app.services import tokens as token_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    old = web_db.database
    web_db.database = database
    yield database
    web_db.database = old
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _token_of(db, purpose) -> str:
    async with db.session() as s:
        row = await s.scalar(
            select(EmailToken).where(EmailToken.purpose == purpose,
                                     EmailToken.used_at.is_(None))
            .order_by(EmailToken.id.desc())
        )
        return row.token if row else None


async def _code_of(db, purpose) -> str:
    async with db.session() as s:
        row = await s.scalar(
            select(EmailToken).where(EmailToken.purpose == purpose,
                                     EmailToken.used_at.is_(None))
            .order_by(EmailToken.id.desc())
        )
        return row.code if row else None


@pytest.mark.asyncio
async def test_verify_email_flow(db):
    async with _client() as c:
        reg = await c.post("/api/auth/register", json={"email": "u@u.com", "password": "secret12"})
        assert reg.status_code == 200
        assert reg.json()["account"]["email_verified"] is False
        tok = await _token_of(db, token_service.VERIFY)
        assert tok
        r = await c.post("/api/auth/verify", json={"token": tok})
        assert r.status_code == 200
        # повторное использование — отказ
        assert (await c.post("/api/auth/verify", json={"token": tok})).status_code == 400
        # /me теперь подтверждён
        login = await c.post("/api/auth/login", json={"email": "u@u.com", "password": "secret12"})
        me = await c.get("/api/auth/me",
                         headers={"Authorization": f"Bearer {login.json()['token']}"})
        assert me.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_verify_by_code(db):
    async with _client() as c:
        reg = await c.post("/api/auth/register", json={"email": "c@c.com", "password": "secret12"})
        assert reg.status_code == 200
        tok = reg.json()["token"]
        hdr = {"Authorization": f"Bearer {tok}"}
        code = await _code_of(db, token_service.VERIFY)
        assert code and len(code) == 6 and code.isdigit()
        # неверный код — 400
        assert (await c.post("/api/auth/verify-code", json={"code": "000000"}, headers=hdr)).status_code == 400
        # верный код — 200 и email подтверждён
        r = await c.post("/api/auth/verify-code", json={"code": code}, headers=hdr)
        assert r.status_code == 200
        me = await c.get("/api/auth/me", headers=hdr)
        assert me.json()["email_verified"] is True


@pytest.mark.asyncio
async def test_code_brute_force_burns(db):
    async with _client() as c:
        reg = await c.post("/api/auth/register", json={"email": "bf@bf.com", "password": "secret12"})
        hdr = {"Authorization": f"Bearer {reg.json()['token']}"}
        code = await _code_of(db, token_service.VERIFY)
        # 5 неверных попыток сжигают код
        for _ in range(token_service.MAX_CODE_ATTEMPTS):
            assert (await c.post("/api/auth/verify-code", json={"code": "111111"}, headers=hdr)).status_code == 400
        # теперь даже верный код не сработает — нужно запросить новый
        assert (await c.post("/api/auth/verify-code", json={"code": code}, headers=hdr)).status_code == 400


@pytest.mark.asyncio
async def test_unverified_blocked_from_topup(db):
    async with _client() as c:
        reg = await c.post("/api/auth/register", json={"email": "b@b.com", "password": "secret12"})
        hdr = {"Authorization": f"Bearer {reg.json()['token']}"}
        # пополнение заблокировано до подтверждения email
        blocked = await c.post("/api/topup", json={"amount": "500", "method": "card"}, headers=hdr)
        assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_verified_account_dependency():
    """Правило блокировки: email без подтверждения — 403; Telegram и подтверждённые — ок."""
    from fastapi import HTTPException
    from web.app.deps import verified_account

    # email-аккаунт без подтверждения → 403
    unverified = WebAccount(email="x@x.com", email_verified=False, telegram_user_id=None)
    with pytest.raises(HTTPException) as ei:
        await verified_account(acc=unverified)
    assert ei.value.status_code == 403

    # подтверждённый email → пропускаем
    verified = WebAccount(email="x@x.com", email_verified=True, telegram_user_id=None)
    assert await verified_account(acc=verified) is verified

    # вошёл через Telegram (даже без подтверждения email) → пропускаем
    tg = WebAccount(email="x@x.com", email_verified=False, telegram_user_id=123)
    assert await verified_account(acc=tg) is tg

    # Telegram без email → пропускаем
    tg_only = WebAccount(email=None, email_verified=False, telegram_user_id=123)
    assert await verified_account(acc=tg_only) is tg_only


@pytest.mark.asyncio
async def test_reset_by_code(db):
    async with _client() as c:
        await c.post("/api/auth/register", json={"email": "rc@rc.com", "password": "oldpass12"})
        assert (await c.post("/api/auth/forgot", json={"email": "rc@rc.com"})).status_code == 200
        code = await _code_of(db, token_service.RESET)
        assert code
        # неверный код — 400
        assert (await c.post("/api/auth/reset-code",
                json={"email": "rc@rc.com", "code": "000000", "password": "newpass99"})).status_code == 400
        # верный код — пароль меняется, аккаунт логинится
        r = await c.post("/api/auth/reset-code",
                         json={"email": "rc@rc.com", "code": code, "password": "newpass99"})
        assert r.status_code == 200 and r.json()["token"]
        assert (await c.post("/api/auth/login", json={"email": "rc@rc.com", "password": "newpass99"})).status_code == 200
        assert (await c.post("/api/auth/login", json={"email": "rc@rc.com", "password": "oldpass12"})).status_code == 401


@pytest.mark.asyncio
async def test_verify_bad_token(db):
    async with _client() as c:
        assert (await c.post("/api/auth/verify", json={"token": "nope"})).status_code == 400


@pytest.mark.asyncio
async def test_forgot_and_reset(db):
    async with _client() as c:
        await c.post("/api/auth/register", json={"email": "r@r.com", "password": "oldpass12"})
        # запрос сброса — всегда 200
        assert (await c.post("/api/auth/forgot", json={"email": "r@r.com"})).status_code == 200
        tok = await _token_of(db, token_service.RESET)
        assert tok
        # смена пароля
        r = await c.post("/api/auth/reset", json={"token": tok, "password": "newpass99"})
        assert r.status_code == 200 and r.json()["token"]
        # новый пароль работает, старый — нет
        assert (await c.post("/api/auth/login", json={"email": "r@r.com", "password": "newpass99"})).status_code == 200
        assert (await c.post("/api/auth/login", json={"email": "r@r.com", "password": "oldpass12"})).status_code == 401
        # токен одноразовый
        assert (await c.post("/api/auth/reset", json={"token": tok, "password": "another12"})).status_code == 400


@pytest.mark.asyncio
async def test_forgot_unknown_email_is_200(db):
    async with _client() as c:
        assert (await c.post("/api/auth/forgot", json={"email": "ghost@x.com"})).status_code == 200
        # токена не создано
        assert await _token_of(db, token_service.RESET) is None


@pytest.mark.asyncio
async def test_reset_short_password_rejected(db):
    async with _client() as c:
        await c.post("/api/auth/register", json={"email": "s@s.com", "password": "oldpass12"})
        await c.post("/api/auth/forgot", json={"email": "s@s.com"})
        tok = await _token_of(db, token_service.RESET)
        r = await c.post("/api/auth/reset", json={"token": tok, "password": "123"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_consume_expiry(db):
    from datetime import datetime, timedelta
    async with db.session() as s:
        s.add(WebAccount(email="e@e.com", password_hash="x"))
        await s.flush()
        s.add(EmailToken(account_id=1, purpose="verify", token="expired",
                         expires_at=datetime.utcnow() - timedelta(hours=1)))
        await s.commit()
    async with db.session() as s:
        assert await token_service.consume(s, "expired", "verify") is None

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

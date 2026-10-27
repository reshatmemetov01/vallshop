"""Аккаунты сайта: регистрация, вход, профиль по JWT."""

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from web.app import db as web_db
from web.app.main import app


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


@pytest.mark.asyncio
async def test_register_login_me(db):
    async with _client() as c:
        # регистрация
        r = await c.post("/api/auth/register", json={"email": "A@Mail.com", "password": "secret12"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["account"]["email"] == "a@mail.com"           # нормализован
        assert data["account"]["telegram_linked"] is False
        token = data["token"]

        # профиль по токену
        me = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["email"] == "a@mail.com"

        # вход
        r2 = await c.post("/api/auth/login", json={"email": "a@mail.com", "password": "secret12"})
        assert r2.status_code == 200 and r2.json()["token"]


@pytest.mark.asyncio
async def test_duplicate_email(db):
    async with _client() as c:
        await c.post("/api/auth/register", json={"email": "x@y.com", "password": "secret12"})
        r = await c.post("/api/auth/register", json={"email": "X@Y.com", "password": "secret12"})
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_bad_inputs(db):
    async with _client() as c:
        assert (await c.post("/api/auth/register", json={"email": "bad", "password": "secret12"})).status_code == 400
        assert (await c.post("/api/auth/register", json={"email": "ok@ok.com", "password": "short"})).status_code == 400


@pytest.mark.asyncio
async def test_login_wrong_password(db):
    async with _client() as c:
        await c.post("/api/auth/register", json={"email": "z@z.com", "password": "secret12"})
        r = await c.post("/api/auth/login", json={"email": "z@z.com", "password": "WRONG123"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_token(db):
    async with _client() as c:
        assert (await c.get("/api/auth/me")).status_code == 401
        assert (await c.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"})).status_code == 401


@pytest.mark.asyncio
async def test_login_page_served(db):
    async with _client() as c:
        for path in ("/login", "/register"):
            r = await c.get(path)
            assert r.status_code == 200
            assert "VallShop" in r.text and "/api/auth/" in r.text

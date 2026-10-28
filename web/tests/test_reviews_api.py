"""API опубликованных отзывов для витрины сайта."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.services import reviews as review_service
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


def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_reviews_only_published_head(db):
    async with db.session() as s:
        # головной: один опубликован, один новый
        pub = await review_service.create_review(
            s, order_id=1, user_id=1, username="buyer", variant_id=1,
            product_title="60 UC", amount_usd=Decimal("5"), rating=5, text="супер",
            merchant_id=0)
        await review_service.set_status(s, pub.id, "published")
        await review_service.create_review(
            s, order_id=2, user_id=2, username="x", variant_id=1,
            product_title="t", amount_usd=Decimal("5"), rating=4, text="new", merchant_id=0)
        # франшиза: опубликован — не должен попасть на сайт головного
        fr = await review_service.create_review(
            s, order_id=3, user_id=3, username="fr", variant_id=1,
            product_title="fr", amount_usd=Decimal("5"), rating=5, text="fr", merchant_id=7)
        await review_service.set_status(s, fr.id, "published")
        await s.commit()
    async with _client() as c:
        r = await c.get("/api/reviews")
        assert r.status_code == 200
        revs = r.json()["reviews"]
        assert len(revs) == 1
        assert revs[0]["text"] == "супер" and revs[0]["rating"] == 5
        assert revs[0]["product_title"] == "60 UC"


@pytest.mark.asyncio
async def test_reviews_empty_ok(db):
    async with _client() as c:
        r = await c.get("/api/reviews")
        assert r.status_code == 200 and r.json()["reviews"] == []

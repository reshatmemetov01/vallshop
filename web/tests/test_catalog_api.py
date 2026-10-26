"""Смоук-тест API каталога сайта (ASGI на общей логике бота)."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import Product, Variant
from web.app import db as web_db
from web.app.main import app


@pytest.fixture
async def seeded():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as s:
        p = Product(game="SMM", title="Подписчики", sort_order=1)
        s.add(p)
        await s.flush()
        s.add(Variant(product_id=p.id, title="Быстрые", liog_product_id=0,
                      liog_variation_id=-1, source="socproof", smm_min=10, smm_max=60000,
                      smm_rate=Decimal("9"), markup_percent=Decimal("40")))
        s.add(Variant(product_id=p.id, title="Ключ", liog_product_id=0,
                      liog_variation_id=1, price_rub=Decimal("299")))
        await s.commit()
        pid = p.id
    old = web_db.database
    web_db.database = database  # сайт ходит в тестовую БД
    yield pid
    web_db.database = old
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_healthz(seeded):
    async with _client() as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.asyncio
async def test_catalog_and_prices(seeded):
    pid = seeded
    async with _client() as c:
        top = (await c.get("/api/catalog")).json()
        assert any(p["title"] == "Подписчики" for p in top["products"])

        det = (await c.get(f"/api/catalog/{pid}")).json()
        assert det["type"] == "product"
        by_title = {v["title"]: v for v in det["variants"]}
        # SMM — цена за 1000 = 9 × 1.40 = 12.60, под заказ
        assert by_title["Быстрые"]["per_1000"] is True
        assert by_title["Быстрые"]["price_rub"] == 12.6
        assert by_title["Быстрые"]["available"] is True
        # обычный ключ с фикс-ценой, но без стока → недоступен
        assert by_title["Ключ"]["price_rub"] == 299
        assert by_title["Ключ"]["available"] is False


@pytest.mark.asyncio
async def test_unknown_product_404(seeded):
    async with _client() as c:
        assert (await c.get("/api/catalog/999999")).status_code == 404


@pytest.mark.asyncio
async def test_storefront_page_served(seeded):
    async with _client() as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "VallShop" in r.text and "/api/catalog" in r.text

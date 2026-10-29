"""SEO: серверные страницы товаров, sitemap.xml, robots.txt, канонические URL."""

from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from bot.db.base import Database
from bot.db.models import Product, Variant
from web.app import db as web_db
from web.app import seo
from web.app.main import app


@pytest.fixture
async def seeded():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as s:
        cat = Product(game="Steam", title="Пополнение Steam", sort_order=1)
        s.add(cat)
        await s.flush()
        leaf = Product(game="Steam", title="Steam Казахстан", parent_id=cat.id, sort_order=1)
        s.add(leaf)
        await s.flush()
        # номинал с фикс-ценой и остатком (в наличии)
        v = Variant(product_id=leaf.id, title="100 ₸", liog_product_id=0,
                    liog_variation_id=1, price_rub=Decimal("150"))
        s.add(v)
        await s.flush()
        from bot.db.models import StockItem
        s.add(StockItem(variant_id=v.id, code="KEY-1"))
        await s.commit()
        ids = {"cat": cat.id, "leaf": leaf.id, "variant": v.id}
    old = web_db.database
    web_db.database = database
    yield ids
    web_db.database = old
    await database.dispose()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_slugify_cyrillic():
    assert seo.slugify("Пополнение Steam") == "popolnenie-steam"
    assert seo.slugify("  !!!  ") == "tovar"
    assert seo.slugify("Telegram Stars 100") == "telegram-stars-100"


@pytest.mark.asyncio
async def test_catalog_css_served():
    async with _client() as c:
        r = await c.get("/catalog.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]


@pytest.mark.asyncio
async def test_robots_txt():
    async with _client() as c:
        r = await c.get("/robots.txt")
        assert r.status_code == 200
        assert "Sitemap:" in r.text
        assert "Disallow: /api/" in r.text


@pytest.mark.asyncio
async def test_sitemap_lists_products(seeded):
    async with _client() as c:
        r = await c.get("/sitemap.xml")
        assert r.status_code == 200
        assert "application/xml" in r.headers["content-type"]
        assert f"/p/{seeded['cat']}-" in r.text
        assert f"/p/{seeded['leaf']}-" in r.text


@pytest.mark.asyncio
async def test_category_page_renders_children(seeded):
    async with _client() as c:
        r = await c.get(f"/p/{seeded['cat']}-popolnenie-steam")
        assert r.status_code == 200
        assert "Пополнение Steam" in r.text
        # ссылка на дочерний раздел присутствует
        assert f"/p/{seeded['leaf']}-" in r.text
        # хлебные крошки и канонический тег
        assert '<link rel="canonical"' in r.text
        assert "BreadcrumbList" in r.text


@pytest.mark.asyncio
async def test_leaf_page_shows_price_and_jsonld(seeded):
    async with _client() as c:
        r = await c.get(f"/p/{seeded['leaf']}-steam-kazahstan")
        assert r.status_code == 200
        assert "150" in r.text                 # цена номинала
        assert "в наличии" in r.text
        assert "AggregateOffer" in r.text      # микроразметка товара
        assert f"/?open={seeded['leaf']}" in r.text  # кнопка «Купить» → витрина


@pytest.mark.asyncio
async def test_wrong_slug_redirects_to_canonical(seeded):
    async with _client() as c:
        r = await c.get(f"/p/{seeded['cat']}-wrong-slug", follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == f"/p/{seeded['cat']}-popolnenie-steam"


@pytest.mark.asyncio
async def test_unknown_product_404(seeded):
    async with _client() as c:
        assert (await c.get("/p/999999-nope")).status_code == 404
        assert (await c.get("/p/abc-nope")).status_code == 404

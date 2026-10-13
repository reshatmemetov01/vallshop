"""Оформление франшизы O2/O3: msettings, баннеры, контакты, сброс."""

import pytest

from bot.db import Database
from bot.services import menu as menu_service
from bot.services import msettings as ms


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    await menu_service.load(database)
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_msettings_roundtrip_and_isolation(db):
    async with db.session() as s:
        await ms.set(s, 1, ms.SUPPORT, "https://t.me/shop1")
        await ms.set(s, 2, ms.SUPPORT, "https://t.me/shop2")
        await s.commit()
    async with db.session() as s:
        assert await ms.get(s, 1, ms.SUPPORT) == "https://t.me/shop1"
        assert await ms.get(s, 2, ms.SUPPORT) == "https://t.me/shop2"
        assert await ms.get(s, 1, ms.URL_OFFER) is None  # не задано
        # all_for — только настройки своего мерчанта
        assert await ms.all_for(s, 1) == {ms.SUPPORT: "https://t.me/shop1"}


@pytest.mark.asyncio
async def test_banner_key_and_grouped(db):
    async with db.session() as s:
        await ms.set(s, 3, ms.banner_key("main"), "FILEID_MAIN")
        await ms.set(s, 3, ms.banner_key("faq"), "FILEID_FAQ")
        await s.commit()
    async with db.session() as s:
        grouped = await ms.all_grouped(s)
    assert grouped[3][ms.banner_key("main")] == "FILEID_MAIN"
    assert grouped[3][ms.banner_key("faq")] == "FILEID_FAQ"


@pytest.mark.asyncio
async def test_reset_keeps_banners_clears_text(db):
    # у франшизы заданы и текст, и баннер
    await menu_service.set_caption(db, "main", "Привет", merchant_id=5)
    async with db.session() as s:
        await ms.set(s, 5, ms.banner_key("main"), "FILEID")
        await s.commit()
    # сброс оформления
    await menu_service.reset_all(db, merchant_id=5)
    cap = menu_service.caption("main", "БАЗА", 5)
    assert cap != "Привет" and "магазин цифровых товаров" in cap  # вернулся нейтральный дефолт
    async with db.session() as s:
        assert await ms.get(s, 5, ms.banner_key("main")) == "FILEID"  # баннер уцелел


@pytest.mark.asyncio
async def test_norm_contact():
    from bot.handlers.merchant_admin import _norm_contact
    assert _norm_contact("@shop") == "https://t.me/shop"
    assert _norm_contact("shop") == "https://t.me/shop"
    assert _norm_contact("https://t.me/shop") == "https://t.me/shop"
    assert _norm_contact("  tg://resolve?domain=x ") == "tg://resolve?domain=x"

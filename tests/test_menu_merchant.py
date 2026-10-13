"""Оформление O1: пер-мерчантные правки меню/текстов + миграция старой БД."""

import sqlite3

import pytest
from sqlalchemy import text

from bot.db import Database
from bot.services import menu as menu_service
from bot.services import msettings as msettings_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    await menu_service.load(database)
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_button_override_is_per_merchant(db):
    # Правка у франшизы 1 не влияет на головной (0) и другую франшизу (2).
    await menu_service.set_label(db, "main.catalog", "Товары", merchant_id=1)
    assert menu_service.resolved("main.catalog", 1).text == "Товары"
    assert menu_service.resolved("main.catalog", 0).text == "Каталог"   # головной
    assert menu_service.resolved("main.catalog", 2).text == "Каталог"   # другая франшиза


@pytest.mark.asyncio
async def test_toggle_per_merchant(db):
    await menu_service.toggle(db, "main.reviews", merchant_id=1)
    vis1 = [r.key for r in menu_service.menu_entries("main", 1)]
    vis0 = [r.key for r in menu_service.menu_entries("main", 0)]
    assert "main.reviews" not in vis1       # скрыто только у франшизы 1
    assert "main.reviews" in vis0           # у головного на месте


@pytest.mark.asyncio
async def test_caption_per_merchant(db):
    await menu_service.set_caption(db, "main", "Привет от франшизы!", merchant_id=1)
    assert menu_service.caption("main", "DEF", 1) == "Привет от франшизы!"
    assert menu_service.caption("main", "DEF", 0) == "DEF"   # головной — дефолт
    # хранится в merchant_settings, не в глобальном settings
    async with db.session() as s:
        assert await msettings_service.get(s, 1, msettings_service.CAP_MAIN) == "Привет от франшизы!"


@pytest.mark.asyncio
async def test_new_franchise_uses_base_template(db):
    # У головного правки есть, у новой франшизы — чистый реестр-дефолт.
    await menu_service.set_label(db, "main.catalog", "МойМагазин", merchant_id=0)
    await menu_service.set_caption(db, "main", "Моё приветствие", merchant_id=0)
    # франшиза 7 без правок
    assert menu_service.resolved("main.catalog", 7).text == "Каталог"
    assert menu_service.caption("main", "БАЗА", 7) == "БАЗА"


@pytest.mark.asyncio
async def test_migration_menu_buttons_and_captions(tmp_path):
    # Старая БД: menu_buttons с одностолбцовым ключом + cap_main в settings.
    dbfile = tmp_path / "legacy_appearance.db"
    con = sqlite3.connect(dbfile)
    con.executescript(
        "CREATE TABLE menu_buttons (key VARCHAR(64) PRIMARY KEY, label VARCHAR(255), "
        "emoji_id VARCHAR(32), enabled BOOLEAN NOT NULL DEFAULT 1, sort_order INTEGER);"
        "INSERT INTO menu_buttons (key, label, enabled) VALUES ('main.catalog', 'Старый', 1);"
        "CREATE TABLE settings (key VARCHAR(64) PRIMARY KEY, value TEXT);"
        "INSERT INTO settings (key, value) VALUES ('cap_main', 'Старое приветствие');"
    )
    con.commit()
    con.close()

    database = Database(f"sqlite+aiosqlite:///{dbfile}")
    await database.create_all()
    try:
        async with database.session() as s:
            info = (await s.execute(text("PRAGMA table_info(menu_buttons)"))).all()
            pk = {r[1] for r in info if r[5] > 0}
            assert pk == {"key", "merchant_id"}       # ключ стал составным
            mid = (await s.execute(
                text("SELECT merchant_id FROM menu_buttons WHERE key='main.catalog'")
            )).scalar()
            assert mid == 0                            # старая правка — у головного
            # заголовок головного перенесён в merchant_settings(0)
            v = await msettings_service.get(s, 0, msettings_service.CAP_MAIN)
            assert v == "Старое приветствие"
        await menu_service.load(database)
        assert menu_service.resolved("main.catalog", 0).text == "Старый"
    finally:
        await database.dispose()

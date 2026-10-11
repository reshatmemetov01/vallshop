"""Способы оплаты: дефолты, вкл/выкл, свой текст, фильтр по доступности провайдера."""

import pytest

from bot.db import Database
from bot.services import paymethods as pm
from bot import texts


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_defaults(db):
    async with db.session() as s:
        methods = await pm.list_methods(s)
    ids = [m["id"] for m in methods]
    assert ids == ["card", "sbp", "usdt"]
    by_id = {m["id"]: m for m in methods}
    assert by_id["card"]["provider"] == "payhot"
    assert by_id["sbp"]["provider"] == "paydex"
    assert by_id["usdt"]["provider"] == "bolt"
    # по умолчанию всё включено и тексты дефолтные
    assert all(m["enabled"] for m in methods)
    assert by_id["sbp"]["label"] == "СБП"
    assert by_id["card"]["label"] == "Банковская карта"


@pytest.mark.asyncio
async def test_toggle_and_label(db):
    async with db.session() as s:
        await pm.set_enabled(s, "card", False)
        await pm.set_label(s, "sbp", "Оплата по СБП ⚡")
        await s.commit()
    async with db.session() as s:
        assert await pm.is_enabled(s, "card") is False
        assert await pm.is_enabled(s, "sbp") is True
        assert await pm.label(s, "sbp") == "Оплата по СБП ⚡"
        # сброс текста к дефолту
        await pm.set_label(s, "sbp", None)
        await s.commit()
    async with db.session() as s:
        assert await pm.label(s, "sbp") == "СБП"


@pytest.mark.asyncio
async def test_heading(db):
    async with db.session() as s:
        assert await pm.heading(s) == texts.TOPUP_CHOOSE_PAYMENT
        await pm.set_heading(s, "Как хотите оплатить?")
        await s.commit()
    async with db.session() as s:
        assert await pm.heading(s) == "Как хотите оплатить?"


@pytest.mark.asyncio
async def test_client_methods_filtered_by_availability(db):
    # СБП включена в админке, но Paydex не настроен → клиенту не показываем.
    available = {"payhot": True, "paydex": False, "bolt": True}
    async with db.session() as s:
        shown = await pm.client_methods(s, available=available)
    ids = [m["id"] for m in shown]
    assert "card" in ids and "usdt" in ids
    assert "sbp" not in ids


@pytest.mark.asyncio
async def test_client_methods_respects_disabled(db):
    available = {"payhot": True, "paydex": True, "bolt": True}
    async with db.session() as s:
        await pm.set_enabled(s, "usdt", False)
        await s.commit()
        shown = await pm.client_methods(s, available=available)
    ids = [m["id"] for m in shown]
    assert ids == ["card", "sbp"]  # usdt выключен админом

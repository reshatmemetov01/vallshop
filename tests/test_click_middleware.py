"""ClickTrackingMiddleware: учитывает клик по кнопке поста и переписывает
callback на реальный раздел (навигация не ломается)."""

import pytest
from aiogram.types import CallbackQuery, User

from bot.db import Database
from bot.db.models import BroadcastClick
from bot.middlewares import ClickTrackingMiddleware
from bot.services import broadcasts as svc


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as s:
        await svc.create(s, merchant_id=0, created_by=1, text="x", photo=None,
                         buttons=[{"label": "A", "kind": "cb", "target": "catalog"}],
                         status="done")
    yield database
    await database.dispose()


def _call(data):
    return CallbackQuery(id="1", from_user=User(id=42, is_bot=False, first_name="u"),
                         chat_instance="ci", data=data)


@pytest.mark.asyncio
async def test_rewrites_and_records(db):
    mw = ClickTrackingMiddleware()
    seen = {}

    async def handler(event, data):
        seen["data"] = event.data

    await mw(handler, _call("bck:1:0:catalog"), {"db": db, "event_from_user": User(id=42, is_bot=False, first_name="u")})
    assert seen["data"] == "catalog"      # переписано на реальный раздел
    async with db.session() as s:
        row = await s.get(BroadcastClick, {"broadcast_id": 1, "btn_idx": 0, "user_id": 42})
        assert row is not None and row.count == 1


@pytest.mark.asyncio
async def test_non_broadcast_callback_untouched(db):
    mw = ClickTrackingMiddleware()
    seen = {}

    async def handler(event, data):
        seen["data"] = event.data

    await mw(handler, _call("catalog"), {"db": db, "event_from_user": User(id=1, is_bot=False, first_name="u")})
    assert seen["data"] == "catalog"

"""Кабинет мерчанта (P1.3): доступ только владельцу франшизы в её боте."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.db import Database, HEAD_MERCHANT_ID, Merchant
from bot.handlers.merchant_admin import IsMerchantAdminCb
from bot.middlewares import MerchantMiddleware
from bot.services import merchants as ms


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _merchant(db, admin_user_id=555):
    async with db.session() as s:
        m = Merchant(title="Shop", bot_token="t:x", bot_username="shop",
                     admin_user_id=admin_user_id, default_markup=Decimal("20"))
        s.add(m)
        await s.commit()
        return m.id


@pytest.mark.asyncio
async def test_admin_merchant_resolver(db):
    mid = await _merchant(db, admin_user_id=555)
    # владелец в своём боте — находится
    found = await ms.admin_merchant(db, 555, mid)
    assert found is not None and found.id == mid
    # чужой пользователь — нет
    assert await ms.admin_merchant(db, 999, mid) is None
    # головной бот (merchant_id=0) кабинета не имеет
    assert await ms.admin_merchant(db, 555, HEAD_MERCHANT_ID) is None


def _call(user_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


@pytest.mark.asyncio
async def test_cabinet_filter(db):
    mid = await _merchant(db, admin_user_id=555)
    flt = IsMerchantAdminCb()
    # владелец франшизы в её боте — доступ есть
    assert await flt(_call(555), db, merchant_id=mid) is True
    # чужой клиент той же франшизы — нет
    assert await flt(_call(999), db, merchant_id=mid) is False
    # тот же владелец, но головной бот — нет кабинета
    assert await flt(_call(555), db, merchant_id=HEAD_MERCHANT_ID) is False


@pytest.mark.asyncio
async def test_merchant_id_reaches_filter_via_outer_middleware(db):
    # Регрессия: merchant_id должен быть в data ДО фильтра (outer middleware),
    # иначе кнопка кабинета «грузит» и не открывается.
    mid = await _merchant(db, admin_user_id=555)
    mw = MerchantMiddleware({222: mid})       # бот 222 → эта франшиза
    captured = {}

    async def handler(event, data):
        flt = IsMerchantAdminCb()
        captured["ok"] = await flt(event, data["db"], data.get("merchant_id", HEAD_MERCHANT_ID))
        return "done"

    data = {"bot": SimpleNamespace(id=222), "db": db}
    await mw(handler, _call(555), data)
    assert data["merchant_id"] == mid          # middleware проставил merchant_id
    assert captured["ok"] is True              # фильтр увидел его и дал доступ

"""Пер-мерчантное оформление каталога: подпись/эмодзи/описание товаров и номиналов."""

from decimal import Decimal

import pytest

from bot.db import Database, HEAD_MERCHANT_ID
from bot.db.models import Product, Variant
from bot.services import mcatalog as mc


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _catalog(db):
    async with db.session() as s:
        p = Product(game="X", title="PUBG", description="Общее описание",
                    icon_emoji_id="111")
        s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="60 UC", liog_product_id=1, liog_variation_id=2,
                    icon_emoji_id="222")
        s.add(v); await s.flush()
        await s.commit()
        return p.id, v.id


@pytest.mark.asyncio
async def test_override_title_emoji_desc_per_merchant(db):
    pid, vid = await _catalog(db)
    async with db.session() as s:
        await mc.set_title(s, 1, mc.PRODUCT, pid, "ПАБГ Моб")
        await mc.set_emoji(s, 1, mc.PRODUCT, pid, "999")
        await mc.set_description(s, 1, mc.PRODUCT, pid, "Моё описание")
        await mc.set_title(s, 1, mc.VARIANT, vid, "60 Юси")
        await s.commit()
    async with db.session() as s:
        p = await s.get(Product, pid); v = await s.get(Variant, vid)
        pov = await mc.get_override(s, 1, mc.PRODUCT, pid)
        vov = await mc.get_override(s, 1, mc.VARIANT, vid)
        # франшиза 1 видит своё
        assert mc.disp_title(p, pov) == "ПАБГ Моб"
        assert mc.disp_emoji(p, pov) == "999"
        assert mc.disp_description(p, pov) == "Моё описание"
        assert mc.disp_title(v, vov) == "60 Юси"
        # головной (0) и другая франшиза (2) — дефолт из каталога
        assert await mc.get_override(s, HEAD_MERCHANT_ID, mc.PRODUCT, pid) is None
        assert mc.disp_title(p, await mc.get_override(s, 2, mc.PRODUCT, pid)) == "PUBG"


@pytest.mark.asyncio
async def test_emoji_empty_means_no_icon_and_clear(db):
    pid, _vid = await _catalog(db)
    async with db.session() as s:
        p = await s.get(Product, pid)
        await mc.set_emoji(s, 1, mc.PRODUCT, pid, "")   # убрать эмодзи
        await s.commit()
        assert mc.disp_emoji(p, await mc.get_override(s, 1, mc.PRODUCT, pid)) is None
        # сброс — возврат к каталожному эмодзи
        await mc.clear(s, 1, mc.PRODUCT, pid)
        await s.commit()
        assert mc.disp_emoji(p, await mc.get_override(s, 1, mc.PRODUCT, pid)) == "111"


@pytest.mark.asyncio
async def test_overrides_map_batch_and_head_empty(db):
    pid, vid = await _catalog(db)
    async with db.session() as s:
        await mc.set_title(s, 1, mc.PRODUCT, pid, "A")
        await s.commit()
        m = await mc.overrides_map(s, 1, mc.PRODUCT, [pid])
        assert m[pid]["title"] == "A"
        # головной — без переопределений
        assert await mc.overrides_map(s, HEAD_MERCHANT_ID, mc.PRODUCT, [pid]) == {}


@pytest.mark.asyncio
async def test_keyboard_applies_override(db):
    from bot import keyboards as kb
    pid, vid = await _catalog(db)
    async with db.session() as s:
        p = await s.get(Product, pid)
        ov = {pid: {"title": "МойТовар", "emoji_id": "", "description": None}}
        m = kb.products_kb([p], overrides=ov)
    btns = [b for row in m.inline_keyboard for b in row]
    main = [b for b in btns if b.callback_data == f"prod:{pid}"][0]
    assert main.text == "МойТовар"

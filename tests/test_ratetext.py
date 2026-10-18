"""Подстановка курса с наценкой магазина в тексте ({курс:1.38})."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant
from bot.services import mprice as mprice_service
from bot.services import ratetext as rt


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_integration_head_vs_franchise(db):
    """Один базовый текст → у головного базовый курс, у франшизы с наценкой."""
    async with db.session() as s:
        m = Merchant(title="F", bot_token="t", bot_username="f", admin_user_id=1,
                     default_markup=Decimal("10"))
        s.add(m)
        await s.commit()
        fid = m.id
    desc = "Курс {курс:1.38}₽ - 1 Звезда"
    async with db.session() as s:
        dm_head = await mprice_service.default_markup(s, 0)      # головной → 0
        dm_fr = await mprice_service.default_markup(s, fid)      # франшиза → 10
    assert rt.apply_markup(desc, dm_head) == "Курс 1.38₽ - 1 Звезда"
    assert rt.apply_markup(desc, dm_fr) == "Курс 1.52₽ - 1 Звезда"


def test_head_markup_zero_keeps_base():
    assert rt.apply_markup("Курс {курс:1.38}₽ - 1 Звезда", 0) == "Курс 1.38₽ - 1 Звезда"


def test_franchise_markup_scales():
    # 1.38 × 1.10 = 1.518 → округл. до копеек 1.52
    assert rt.apply_markup("Курс {курс:1.38}₽", Decimal("10")) == "Курс 1.52₽"


def test_trailing_zeros_trimmed():
    # 2.00 → "2", 1.50 → "1.5"
    assert rt.apply_markup("{курс:2}", 0) == "2"
    assert rt.apply_markup("{курс:1.00}", Decimal("50")) == "1.5"  # 1 × 1.5


def test_comma_and_spaces_and_case():
    assert rt.apply_markup("{КУРС: 1,38 }", 0) == "1.38"


def test_multiple_and_other_text_untouched():
    out = rt.apply_markup("A {курс:1} B {курс:2} C", Decimal("100"))
    assert out == "A 2 B 4 C"


def test_no_placeholder_returns_as_is():
    assert rt.apply_markup("Просто текст 1.38", Decimal("10")) == "Просто текст 1.38"
    assert rt.apply_markup(None, Decimal("10")) is None

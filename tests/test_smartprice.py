"""Умная цена: вывод наценки, пересчёт с порогом, стоп-лосс, массовый перевод."""

from decimal import Decimal

import pytest

from bot.db import Database, Product, Variant
from bot.services import smartprice
from fazercard import FazerCardClient


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


def test_target_rub_ceiling():
    # ceil(1.00 × 95 × 1.5) = ceil(142.5) = 143  (≥100 → до целого)
    assert smartprice.target_rub(Decimal("1.00"), Decimal("50"), Decimal("95")) == Decimal("143")


def test_round_price_rub_cheap_vs_expensive():
    from bot.services.pricing import round_price_rub
    # < 100 ₽ → округление вверх до 10 копеек (всегда оканчивается на 0)
    assert round_price_rub(Decimal("78.37")) == Decimal("78.40")
    assert round_price_rub(Decimal("78.41")) == Decimal("78.50")
    assert round_price_rub(Decimal("78.00")) == Decimal("78.0")
    # ≥ 100 ₽ → до целого рубля
    assert round_price_rub(Decimal("142.50")) == Decimal("143")
    assert round_price_rub(Decimal("290.00")) == Decimal("290")


def test_target_rub_cheap_dime_step():
    # 0.50 × 95 × 1.5 = 71.25 → <100 → вверх до 10 коп → 71.30
    assert smartprice.target_rub(Decimal("0.50"), Decimal("50"), Decimal("95")) == Decimal("71.30")


def test_derive_and_target_roundtrip():
    rate, cost = Decimal("95"), Decimal("1.00")
    mk = smartprice.derive_markup(Decimal("143"), cost, rate)
    assert smartprice.target_rub(cost, mk, rate) == Decimal("143")


async def _mk(db, *, markup, price_rub, mode="smart", price_usd=None, cost_usd=Decimal("1.00")):
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(
            product_id=p.id, title="60 UC", liog_product_id=0, liog_variation_id=-1,
            cost_usd=cost_usd, source="fazercard", fzr_kind="topup",
            fzr_a="free_fire", fzr_b="o1",
            markup_percent=markup, price_rub=price_rub, price_usd=price_usd,
            price_mode=mode,
        )
        s.add(v); await s.commit()
        return v.id


@pytest.mark.asyncio
async def test_recompute_within_tolerance_no_change(db):
    fzr = FazerCardClient(mock=True)  # mock topup o1 = $1.00 → цель при 50% = 143 ₽
    vid = await _mk(db, markup=Decimal("50"), price_rub=Decimal("143"))
    changes = await smartprice.recompute_all(db, fzr)
    assert changes == []
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert v.price_rub == Decimal("143")


@pytest.mark.asyncio
async def test_recompute_updates_when_cost_moves(db):
    fzr = FazerCardClient(mock=True)  # цель 143, а цена стоит 100 → подтянется вверх
    vid = await _mk(db, markup=Decimal("50"), price_rub=Decimal("100"))
    changes = await smartprice.recompute_all(db, fzr)
    assert len(changes) == 1
    _id, _title, old, new = changes[0]
    assert old == Decimal("100") and new == Decimal("143")
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert v.price_rub == Decimal("143")


class _FakeBot:
    """Записывает отправленные сообщения вместо реальной отправки в Telegram."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, *args, **kwargs):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_alert_on_cost_drop(db):
    # Было $1.02 (mock o1 даёт $1.00) → падение ~1.96% > 0.5% → алерт о падении.
    fzr = FazerCardClient(mock=True)
    await _mk(db, markup=Decimal("50"), price_rub=Decimal("143"), cost_usd=Decimal("1.02"))
    bot = _FakeBot()
    await smartprice.recompute_all(db, fzr, bot=bot, admin_ids=[1])
    assert len(bot.messages) == 1
    _chat, text = bot.messages[0]
    assert "Цены упали" in text
    assert "0.5" not in text or "−" in text  # есть процент падения
    assert "без изменений" in text  # цена в пределах порога 1% не сдвинулась


@pytest.mark.asyncio
async def test_no_drop_alert_below_half_percent(db):
    # Было $1.004 → падение ~0.4% < 0.5% → алерта нет.
    fzr = FazerCardClient(mock=True)
    await _mk(db, markup=Decimal("50"), price_rub=Decimal("143"), cost_usd=Decimal("1.004"))
    bot = _FakeBot()
    await smartprice.recompute_all(db, fzr, bot=bot, admin_ids=[1])
    assert bot.messages == []


@pytest.mark.asyncio
async def test_stop_loss(db):
    fzr = FazerCardClient(mock=True)  # закуп $1.00 × 95 = 95 ₽
    async with db.session() as s:
        p = Product(game="X", title="T"); s.add(p); await s.flush()
        v = Variant(product_id=p.id, title="v", liog_product_id=0, liog_variation_id=-1,
                    cost_usd=Decimal("1.00"), source="fazercard", fzr_kind="topup",
                    fzr_a="free_fire", fzr_b="o1", price_mode="smart")
        s.add(v); await s.commit()
        vid = v.id
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert await smartprice.stop_loss_violation(fzr, v, Decimal("50")) is True   # 50 < 95
        assert await smartprice.stop_loss_violation(fzr, v, Decimal("200")) is False  # 200 ≥ 95


@pytest.mark.asyncio
async def test_migrate_supplier_to_smart(db):
    fzr = FazerCardClient(mock=True)
    # float-номинал: price_usd=1.50 → текущая ₽-цена = ceil(1.50×95)=143
    vid = await _mk(db, markup=None, price_rub=None, mode="float", price_usd=Decimal("1.50"))
    converted, skipped = await smartprice.migrate_supplier_to_smart(db, fzr, Decimal("0"))
    assert converted == 1
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert v.price_mode == "smart"
        assert v.price_rub == Decimal("143")
        assert v.markup_percent is not None
    # повторный прогон уже ничего не переводит (уже умная)
    converted2, _ = await smartprice.migrate_supplier_to_smart(db, fzr, Decimal("0"))
    assert converted2 == 0


@pytest.mark.asyncio
async def test_migrate_skips_fixed(db):
    fzr = FazerCardClient(mock=True)
    await _mk(db, markup=None, price_rub=Decimal("500"), mode="fixed")
    converted, skipped = await smartprice.migrate_supplier_to_smart(db, fzr, Decimal("0"))
    assert converted == 0 and skipped == 1

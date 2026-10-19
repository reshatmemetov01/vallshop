"""SMM-расчёты (цена за 1000 в ₽, лимиты, возвраты, прогресс) и пакетное добавление."""

from decimal import Decimal

import pytest

from bot.db import Database
from bot.db.models import Product, Variant
from bot.services import catalog as catalog_service
from bot.services import smm


# ── Чистая математика ────────────────────────────────────────────────────────
def test_sell_per_1000():
    assert smm.sell_per_1000_rub("9.00", 0) == Decimal("9.00")
    assert smm.sell_per_1000_rub("9.00", 50) == Decimal("13.50")
    assert smm.sell_per_1000_rub("16", 25) == Decimal("20.00")


def test_order_total():
    # 13.5₽/1000 × 100 = 1.35₽
    assert smm.order_total_rub("13.50", 100) == Decimal("1.35")
    assert smm.order_total_rub("13.50", 1000) == Decimal("13.50")
    assert smm.order_total_rub("9.00", 500) == Decimal("4.50")


def test_validate_quantity():
    assert smm.validate_quantity("50", 10, 1000) is None
    assert "Минимум" in smm.validate_quantity("5", 10, 1000)
    assert "Максимум" in smm.validate_quantity("2000", 10, 1000)
    assert smm.validate_quantity("abc", 10, 1000) is not None
    assert smm.validate_quantity("0", 0, 0) is not None


def test_refund_amount():
    total = Decimal("100.00")
    # полный возврат (отмена)
    assert smm.refund_amount_rub(total, 1000, 1000, full=True) == Decimal("100.00")
    # частично: доставлено 600 из 1000 → возврат за 400
    assert smm.refund_amount_rub(total, 1000, 400, full=False) == Decimal("40.00")
    # remains больше qty — клампится
    assert smm.refund_amount_rub(total, 1000, 5000, full=False) == Decimal("100.00")
    # remains 0 — ничего не возвращаем
    assert smm.refund_amount_rub(total, 1000, 0, full=False) == Decimal("0.00")


def test_progress_percent():
    assert smm.progress_percent(1000, 1000) == 0
    assert smm.progress_percent(1000, 400) == 60
    assert smm.progress_percent(1000, 0) == 100
    assert smm.progress_percent(1000, 5000) == 0  # клампится


def test_status_label():
    assert "Выполнен" in smm.status_label("Completed")
    assert "Частично" in smm.status_label("Partial")
    assert "Отменён" in smm.status_label("Canceled")
    assert "очереди" in smm.status_label("Pending")


# ── Пакетное создание SMM-услуг ──────────────────────────────────────────────
@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _product(db):
    async with db.session() as s:
        p = Product(game="SMM", title="Telegram накрутка")
        s.add(p)
        await s.commit()
        return p.id


def _svc(sid, name, rate, lo=10, hi=1000):
    return {"id": sid, "name": name, "rate": rate, "min": lo, "max": hi, "type": "Default"}


@pytest.mark.asyncio
async def test_bulk_create_smm(db):
    pid = await _product(db)
    services = [
        _svc(707, "Подписчики", "9.00", 10, 60000),
        _svc(500, "Просмотры", "3.00", 100, 150000),
    ]
    async with db.session() as s:
        added, skipped = await catalog_service.bulk_create_smm_variants(
            s, pid, services, Decimal("50")
        )
    assert (added, skipped) == (2, 0)
    async with db.session() as s:
        variants = await catalog_service.list_variants(s, pid, only_active=False)
    assert [v.smm_service for v in variants] == [707, 500]
    v0 = variants[0]
    assert v0.source == "socproof"
    assert v0.smm_rate == Decimal("9.0000")
    assert v0.markup_percent == Decimal("50")
    assert v0.smm_min == 10 and v0.smm_max == 60000
    assert v0.sort_order < variants[1].sort_order
    # уникальные отрицательные liog_variation_id
    vids = [v.liog_variation_id for v in variants]
    assert len(set(vids)) == 2 and all(x < 0 for x in vids)


@pytest.mark.asyncio
async def test_bulk_create_smm_skips_duplicates(db):
    pid = await _product(db)
    async with db.session() as s:
        s.add(Variant(
            product_id=pid, title="Подписчики", liog_product_id=0, liog_variation_id=-1,
            source="socproof", smm_service=707, smm_rate=Decimal("9"), sort_order=3,
        ))
        await s.commit()
    services = [_svc(707, "Подписчики", "9.00"), _svc(500, "Просмотры", "3.00")]
    async with db.session() as s:
        added, skipped = await catalog_service.bulk_create_smm_variants(
            s, pid, services, Decimal("0")
        )
    assert (added, skipped) == (1, 1)
    async with db.session() as s:
        variants = await catalog_service.list_variants(s, pid, only_active=False)
    assert sorted(v.smm_service for v in variants) == [500, 707]
    new = next(v for v in variants if v.smm_service == 500)
    assert new.sort_order > 3  # после существующего

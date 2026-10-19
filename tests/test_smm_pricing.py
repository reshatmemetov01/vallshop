"""SMM: доступность услуги у поставщика и автопересчёт цены при изменении закупа."""

from decimal import Decimal

import pytest

from bot.db import Database
from bot.db.models import Product, Variant
from bot.services import catalog as catalog_service
from bot.services import smartprice as smartprice_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


class FakeSoc:
    configured = True

    def __init__(self, services):
        self._services = services

    def services(self, force=False):
        return self._services


async def _variant(db, service=707, rate="9.00", markup="50"):
    async with db.session() as s:
        p = Product(game="SMM", title="TG")
        s.add(p)
        await s.flush()
        v = Variant(
            product_id=p.id, title="Подписчики", liog_product_id=0, liog_variation_id=-1,
            source="socproof", smm_service=service, smm_min=10, smm_max=1000,
            smm_rate=Decimal(rate), markup_percent=Decimal(markup),
        )
        s.add(v)
        await s.commit()
        return v.id


# ── Доступность ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_socproof_available(db):
    vid = await _variant(db, service=707)
    async with db.session() as s:
        variants = await catalog_service.list_variants(s, (await s.get(Variant, vid)).product_id, only_active=False)
    # услуга есть у поставщика
    soc = FakeSoc([{"id": 707, "rate": "9"}])
    assert (await catalog_service.socproof_available(soc, variants)) == {vid: True}
    # услуга пропала
    soc2 = FakeSoc([{"id": 999, "rate": "9"}])
    assert (await catalog_service.socproof_available(soc2, variants)) == {vid: False}


@pytest.mark.asyncio
async def test_socproof_available_failopen(db):
    vid = await _variant(db)
    async with db.session() as s:
        variants = await catalog_service.list_variants(s, (await s.get(Variant, vid)).product_id, only_active=False)

    class Broken:
        configured = True
        def services(self, force=False):
            raise RuntimeError("API down")

    # сбой API → пусто (fail-open, не блокируем продажи)
    assert (await catalog_service.socproof_available(Broken(), variants)) == {}
    # не настроен → пусто
    assert (await catalog_service.socproof_available(None, variants)) == {}


# ── Автопересчёт цены ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_recompute_updates_rate_on_change(db):
    vid = await _variant(db, service=707, rate="9.00", markup="50")
    # закуп вырос 9 → 12 (> 1%)
    soc = FakeSoc([{"id": 707, "rate": "12.00"}])
    changes = await smartprice_service.recompute_socproof(db, soc)
    assert len(changes) == 1
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert v.smm_rate == Decimal("12.0000")   # закуп обновился → цена пересчитается


@pytest.mark.asyncio
async def test_recompute_ignores_small_change(db):
    vid = await _variant(db, service=707, rate="100.00", markup="50")
    # закуп 100 → 100.5 (0.5% < 1%) — не трогаем
    soc = FakeSoc([{"id": 707, "rate": "100.50"}])
    changes = await smartprice_service.recompute_socproof(db, soc)
    assert changes == []
    async with db.session() as s:
        assert (await s.get(Variant, vid)).smm_rate == Decimal("100.0000")


def test_order_line_robust_to_bad_fields_json():
    """Регрессия: «Мои заказы» не падают, если fields_json не словарь/пустой."""
    import json
    from bot.handlers.profile import _order_line
    from bot.db.models import OrderStatus

    class O:
        def __init__(self, **k):
            self.id = 1; self.price_usd = Decimal("1.01"); self.quantity = 1
            self.supplier = "socproof"; self.status = OrderStatus.FULFILLING
            self.fail_reason = None; self.delivery_code = None; self.fields_json = None
            self.__dict__.update(k)

    # список вместо словаря — раньше крашило AttributeError
    assert "Быстрые" in _order_line(O(fields_json=json.dumps([{"a": 1}])), "Быстрые")
    # None fields_json
    assert "Быстрые" in _order_line(O(fields_json=None), "Быстрые")
    # price None
    assert "0 ₽" in _order_line(O(price_usd=None), "Быстрые")


@pytest.mark.asyncio
async def test_variant_description_persists(db):
    vid = await _variant(db)
    async with db.session() as s:
        v = await s.get(Variant, vid)
        assert v.description is None       # по умолчанию нет
        v.description = "Запуск 0-1ч · скорость 20к/сутки · гарантия 3 дня"
        await s.commit()
    async with db.session() as s:
        assert (await s.get(Variant, vid)).description.startswith("Запуск")


@pytest.mark.asyncio
async def test_recompute_skips_missing_service(db):
    vid = await _variant(db, service=707, rate="9.00")
    # услуги 707 нет в выдаче — закуп не трогаем (это обрабатывает «наличие»)
    soc = FakeSoc([{"id": 1, "rate": "5"}])
    changes = await smartprice_service.recompute_socproof(db, soc)
    assert changes == []
    async with db.session() as s:
        assert (await s.get(Variant, vid)).smm_rate == Decimal("9.0000")

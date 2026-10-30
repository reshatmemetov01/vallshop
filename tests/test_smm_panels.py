"""Мультипанельность SMM: заказ уходит в нужную панель (socproof / onexpanel)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from bot.db import Database
from bot.db.models import Order, OrderStatus, Product, Variant
from bot.services import balance as balance_service
from bot.services import catalog as catalog_service
from bot.services import orders as order_service
from bot.services import smm, smm_panels


def test_registry_basics():
    assert smm_panels.is_smm("socproof") and smm_panels.is_smm("onexpanel")
    assert not smm_panels.is_smm("fazercard") and not smm_panels.is_smm(None)
    assert smm_panels.label("onexpanel") == "1xpanel"
    clients = smm_panels.build_clients({"ONEXPANEL_API_KEY": "k", "SOCPROOF_API_KEY": "s"})
    assert set(clients) == {"socproof", "onexpanel"}
    assert clients["onexpanel"].base_url == "https://1xpanel.com/api/v2"
    assert set(smm_panels.configured_panels(clients)) == {"socproof", "onexpanel"}


class _FakePanel:
    configured = True

    def __init__(self, order_id):
        self._oid = order_id
        self.added = []

    def add(self, *, service, link, quantity):
        self.added.append((service, link, quantity))
        return {"order": self._oid}

    def status(self, order):
        return {"status": "Completed", "remains": 0}


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _smm_variant(db, source):
    async with db.session() as s:
        p = Product(game="SMM", title=f"P-{source}")
        s.add(p)
        await s.flush()
        v = Variant(product_id=p.id, title="svc", liog_product_id=0, liog_variation_id=-1,
                    source=source, smm_service=707, smm_min=10, smm_max=60000,
                    smm_rate=Decimal("9"), markup_percent=Decimal("50"))
        s.add(v)
        await s.flush()
        await order_service.ensure_user(s, 1, "u", "U", 0)
        await balance_service.credit(s, 1, Decimal("1000"), merchant_id=0)
        await s.commit()
        return v.id


@pytest.mark.asyncio
async def test_order_routes_to_its_panel(db):
    vid = await _smm_variant(db, "onexpanel")
    soc, onex = _FakePanel(111), _FakePanel(222)
    panels = {"socproof": soc, "onexpanel": onex}
    async with db.session() as s:
        v = await s.get(Variant, vid)
        total = smm.order_total_rub(smm.sell_per_1000_rub(v.smm_rate, v.markup_percent), 1000)
        order, _ = await order_service.purchase_from_balance(
            s, user_id=1, variant=v, unit_price=total, quantity=1, merchant_id=0,
            owner_unit_price=total, panels=panels, smm_link="https://t.me/x", smm_quantity=1000,
        )
        await s.commit()
        oid = order.id
    # ушло в 1xpanel, не в soc-proof; supplier записан как onexpanel
    assert onex.added and not soc.added
    async with db.session() as s:
        o = await s.get(Order, oid)
        assert o.supplier == "onexpanel"
        assert o.supplier_order_id == "222"
        assert o.status == OrderStatus.FULFILLING


@pytest.mark.asyncio
async def test_bulk_create_smm_source(db):
    async with db.session() as s:
        p = Product(game="SMM", title="Bulk")
        s.add(p)
        await s.flush()
        services = [{"id": 1, "name": "A", "rate": "5", "min": 10, "max": 100}]
        added, _ = await catalog_service.bulk_create_smm_variants(
            s, p.id, services, Decimal("30"), source="onexpanel"
        )
        await s.commit()
        assert added == 1
        v = (await s.scalars(select(Variant))).first()
    assert v.source == "onexpanel"


@pytest.mark.asyncio
async def test_smm_available_per_panel(db):
    vid = await _smm_variant(db, "onexpanel")

    class _Svc:
        configured = True

        def services(self):
            return [{"id": 707}]      # услуга существует в 1xpanel

    async with db.session() as s:
        v = await s.get(Variant, vid)
        avail = await catalog_service.smm_available({"onexpanel": _Svc()}, [v])
    assert avail == {vid: True}

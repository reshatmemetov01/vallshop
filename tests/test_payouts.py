"""Выплаты франшизам: кошельки, заявки (резерв), одобрение/отклонение (возврат)."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant, MerchantPayout
from bot.services import payouts as pay


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _merchant(db, earnings="1000"):
    async with db.session() as s:
        m = Merchant(title="A", bot_token="t:x", bot_username="a", admin_user_id=1,
                     earnings_rub=Decimal(earnings), earnings_total_rub=Decimal(earnings))
        s.add(m); await s.commit()
        return m.id


@pytest.mark.asyncio
async def test_wallets_bind(db):
    mid = await _merchant(db)
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+79001234567", "Сбер")
        await pay.set_usdt(s, mid, "0xABCDEF0123456789")
        await s.commit()
        w = await pay.wallets(s, mid)
    assert w["sbp"] == {"phone": "+79001234567", "bank": "Сбер"}
    assert w["usdt"] == "0xABCDEF0123456789"
    assert pay.wallet_label("sbp", w) == "СБП +79001234567 (Сбер)"
    assert pay.wallet_label("usdt", w) == "USDT BEP20 0xABCDEF0123456789"


@pytest.mark.asyncio
async def test_create_request_validations(db):
    mid = await _merchant(db, earnings="400")
    async with db.session() as s:
        # нет кошелька
        p, err = await pay.create_request(s, mid, Decimal("500"), "sbp")
        assert p is None and "привяжите" in err.lower()
        await pay.set_sbp(s, mid, "+7900", "Сбер"); await s.commit()
    async with db.session() as s:
        # ниже минимума
        p, err = await pay.create_request(s, mid, Decimal("100"), "sbp")
        assert p is None and "минимал" in err.lower()
        # больше баланса (400)
        p, err = await pay.create_request(s, mid, Decimal("500"), "sbp")
        assert p is None and "недостаточно" in err.lower()


@pytest.mark.asyncio
async def test_request_reserves_and_approve(db):
    mid = await _merchant(db, earnings="1000")
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+7900", "Сбер")
        p, err = await pay.create_request(s, mid, Decimal("600"), "sbp")
        await s.commit()
        assert err is None and p.status == "pending"
        pid = p.id
        # сумма зарезервирована (списана с доступного)
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("400")
        # total не изменился
        assert (await s.get(Merchant, mid)).earnings_total_rub == Decimal("1000")
    async with db.session() as s:
        done = await pay.approve(s, pid)
        await s.commit()
        assert done.status == "approved"
        # баланс не меняется при одобрении (уже зарезервировано)
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("400")
        # повторное одобрение — нет
        assert await pay.approve(s, pid) is None


class _FakeState:
    def __init__(self):
        self._d = {}
        self._s = None

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **k):
        self._d.update(k)
        return dict(self._d)

    async def set_state(self, s):
        self._s = s

    async def clear(self):
        self._d = {}
        self._s = None

    @property
    def state(self):
        return self._s


class _FakeMsgObj:
    def __init__(self):
        self.outs = []

    async def edit_text(self, text, reply_markup=None):
        self.outs.append((text, reply_markup))


class _FakeUser:
    id = 1


class _FakeCall:
    def __init__(self, data=""):
        self.data = data
        self.message = _FakeMsgObj()
        self.from_user = _FakeUser()

    async def answer(self, *a, **k):
        pass


class _FakeInMsg:
    def __init__(self, text):
        self.text = text
        self.from_user = _FakeUser()
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class _Cfg:
    admin_ids = []


@pytest.mark.asyncio
async def test_withdraw_prompts_even_when_below_min(db):
    """Доступно < минимума — всё равно спрашиваем сумму (не тупик)."""
    from bot.handlers import merchant_admin as ma
    mid = await _merchant(db, earnings="0")
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+7900", "Сбер")
        await s.commit()
    state = _FakeState()
    await ma.cb_pf_withdraw(_FakeCall("pf_wd"), db, state, merchant_id=mid)
    assert state.state == ma.MerchAdmin.pf_amount  # состояние ввода суммы включено


@pytest.mark.asyncio
async def test_amount_error_has_back_button(db):
    """Ошибка при вводе суммы приходит с кнопкой «Назад»."""
    from bot.handlers import merchant_admin as ma
    mid = await _merchant(db, earnings="0")
    async with db.session() as s:
        await pay.set_sbp(s, mid, "+7900", "Сбер")
        await s.commit()
    state = _FakeState()
    await state.update_data(method="sbp")
    msg = _FakeInMsg("500")  # 500 > доступно 0 → недостаточно средств
    await ma.msg_pf_amount(msg, db, _Cfg(), state, bots_by_merchant={}, merchant_id=mid)
    text, markup = msg.answers[-1]
    assert "недостаточно" in text.lower()
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "pf_home" in cbs


@pytest.mark.asyncio
async def test_reject_refunds(db):
    mid = await _merchant(db, earnings="1000")
    async with db.session() as s:
        await pay.set_usdt(s, mid, "0x1234567890")
        p, _ = await pay.create_request(s, mid, Decimal("700"), "usdt")
        await s.commit()
        pid = p.id
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("300")
    async with db.session() as s:
        r = await pay.reject(s, pid, "неверные реквизиты")
        await s.commit()
        assert r.status == "rejected" and r.reason == "неверные реквизиты"
        # сумма вернулась на баланс
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("1000")

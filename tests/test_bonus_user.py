"""Пользовательские хендлеры бонусов: клик по дропу и ввод промокода."""

from decimal import Decimal

import pytest

from bot.db import Database, User
from bot.db.models import BonusDropMessage
from bot.services import balance as balance_service
from bot.services import bonuses as bn
from bot.handlers import bonus_user as bu


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as s:
        s.add(User(id=1, merchant_id=0))
        s.add(User(id=2, merchant_id=0))
        await s.commit()
    yield database
    await database.dispose()


class _Chat:
    def __init__(self, i):
        self.id = i


class _Sent:
    def __init__(self):
        self.chat = _Chat(777)
        self.message_id = 42


class _CallMsg:
    def __init__(self):
        self.chat = _Chat(777)
        self.sent = []

    async def answer(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return _Sent()


class _U:
    def __init__(self, i):
        self.id = i
        self.username = "u"
        self.full_name = "U"


class _Call:
    def __init__(self, data, uid=1):
        self.data = data
        self.message = _CallMsg()
        self.from_user = _U(uid)
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


class _InMsg:
    def __init__(self, text, uid=1):
        self.text = text
        self.from_user = _U(uid)
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class _State:
    async def set_state(self, s):
        pass

    async def clear(self):
        pass


async def _active_drop(db, limit=1, amount="50"):
    async with db.session() as s:
        d = await bn.create_drop(
            s, merchant_id=0, created_by=9, text="t", photo=None, btn_label="go",
            amount_rub=Decimal(amount), max_claims=limit, success_text="Успех!",
            error_text="Не успели", success_btn_label=None, success_btn_kind=None,
            success_btn_target=None,
        )
    await bn.activate_drop(db, d.id, 2)
    return d.id


@pytest.mark.asyncio
async def test_claim_success_sends_and_stores(db):
    did = await _active_drop(db, limit=2)
    call = _Call(f"bclaim:{did}", uid=1)
    await bu.cb_bonus_claim(call, db, merchant_id=0)
    assert call.message.sent and "Успех" in call.message.sent[0][0]
    assert any("зачислен" in (a or "").lower() for a in call.answered)
    async with db.session() as s:
        assert await balance_service.get_balance(s, 1, 0) == Decimal("50")
        msgs = await bn.messages_to_delete(s, did)
        assert len(msgs) == 1  # служебное сообщение сохранено для удаления через 24ч


@pytest.mark.asyncio
async def test_claim_ended_sends_error(db):
    did = await _active_drop(db, limit=1)
    await bu.cb_bonus_claim(_Call(f"bclaim:{did}", uid=1), db, merchant_id=0)  # забрал последний
    call2 = _Call(f"bclaim:{did}", uid=2)
    await bu.cb_bonus_claim(call2, db, merchant_id=0)
    assert call2.message.sent and "Не успели" in call2.message.sent[0][0]
    async with db.session() as s:
        assert await balance_service.get_balance(s, 2, 0) == Decimal("0")


@pytest.mark.asyncio
async def test_claim_already(db):
    did = await _active_drop(db, limit=5)
    await bu.cb_bonus_claim(_Call(f"bclaim:{did}", uid=1), db, merchant_id=0)
    call2 = _Call(f"bclaim:{did}", uid=1)
    await bu.cb_bonus_claim(call2, db, merchant_id=0)
    assert any("уже" in (a or "").lower() for a in call2.answered)


@pytest.mark.asyncio
async def test_promo_redeem_handler(db):
    async with db.session() as s:
        promo = await bn.create_promo(s, merchant_id=0, created_by=9,
                                      amount_rub=Decimal("100"), max_activations=0)
        await s.commit()
        code = promo.code
    msg = _InMsg(code, uid=1)
    await bu.msg_promo_code(msg, db, _State(), merchant_id=0)
    assert any("начислено" in (t or "").lower() for t, _ in msg.answers)
    async with db.session() as s:
        assert await balance_service.get_balance(s, 1, 0) == Decimal("100")

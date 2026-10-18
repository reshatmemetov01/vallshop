"""Визард бонус-рассылки (bonus_flow): проход черновика → создание дропа + рассылка."""

import asyncio
from decimal import Decimal

import pytest
from aiogram import Router
from aiogram.fsm.state import State, StatesGroup

from bot.db import Database, Merchant, User
from bot.services import bonuses as bn
from bot.handlers import bonus_flow, broadcast_flow
from bot import texts


async def _merchant(db, earnings="0"):
    async with db.session() as s:
        m = Merchant(title="F", bot_token="t", bot_username="f", admin_user_id=1,
                     earnings_rub=Decimal(earnings), earnings_total_rub=Decimal(earnings))
        s.add(m)
        await s.commit()
        return m.id


class _BStates(StatesGroup):
    promo_amount = State(); promo_limit = State()
    d_post = State(); d_amount = State(); d_limit = State(); d_btnlabel = State()
    d_success = State(); d_surl = State(); d_slabel = State(); d_error = State()


class _State:
    def __init__(self):
        self._d = {}

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **k):
        self._d.update(k)
        return dict(self._d)

    async def set_state(self, s):
        pass

    async def clear(self):
        self._d = {}


class _U:
    def __init__(self, i=9):
        self.id = i
        self.username = "a"
        self.full_name = "A"


class _Sent:
    class _C:
        id = 1
    chat = _C()
    message_id = 7


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, reply_markup=None):
        self.sent.append(uid)
        return _Sent()

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self.sent.append(uid)
        return _Sent()


class _CallMsg:
    def __init__(self, bot):
        self.bot = bot
        self.outs = []

    async def edit_text(self, text, reply_markup=None):
        self.outs.append(text)

    async def answer(self, text, reply_markup=None):
        self.outs.append(text)


class _Call:
    def __init__(self, data, bot):
        self.data = data
        self.message = _CallMsg(bot)
        self.from_user = _U()
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.caption = None
        self.photo = None
        self.from_user = _U()
        self.answers = []

    @property
    def html_text(self):
        return self.text

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.answers.append("photo")


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


@pytest.mark.asyncio
async def test_drop_wizard_creates_and_sends(db):
    h = bonus_flow.register(Router(), prefix="hbn", home_cb="st_home",
                            sections=broadcast_flow.HEAD_SECTIONS, states=_BStates)
    bot = _Bot()
    st = _State()
    await h["drop_new"](_Call("hbn:dnew", bot), st)
    await h["d_post"](_Msg("Лови бонус!"), st)
    await h["d_amount"](_Msg("50"), st)
    await h["d_limit"](_Msg("2"), st)
    await h["d_btnlabel"](_Msg("Забрать"), st)
    await h["d_success"](_Msg("Успех"), st)
    await h["sb_none"](_Call("hbn:sbnone", bot), st)
    await h["d_error"](_Msg("0"), st)           # стандартный текст ошибки
    await h["drop_send"](_Call("hbn:dsend", bot), db, st, merchant_id=0)
    await h["drop_go"](_Call("hbn:dgo", bot), db, st, merchant_id=0)
    await asyncio.sleep(0.5)                    # дать фоновой рассылке отработать

    async with db.session() as s:
        drops = await bn.recent_drops(s, 0)
    assert len(drops) == 1
    d = drops[0]
    assert d.amount_rub == Decimal("50")
    assert d.max_claims == 2
    assert d.btn_label == "Забрать"
    assert d.error_text == texts.BONUS_ENDED_DEFAULT
    assert d.status == "active"                 # разослан, приём активаций открыт
    # пост ушёл обоим клиентам
    assert 1 in bot.sent and 2 in bot.sent


def _flow(states=_BStates, prefix="mbn"):
    return bonus_flow.register(Router(), prefix=prefix, home_cb="ma_home",
                               sections=broadcast_flow.FRANCHISE_SECTIONS, states=states)


@pytest.mark.asyncio
async def test_fr_promo_blocked_when_low_balance(db):
    mid = await _merchant(db, earnings="50")
    h = _flow()
    st = _State()
    await st.update_data(p_amount="100")
    msg = _Msg("2")  # лимит 2 → нужно 200 > 50
    await h["promo_limit"](msg, db, st, merchant_id=mid)
    assert any("Недостаточно" in a for a in msg.answers)
    async with db.session() as s:
        assert await bn.list_promos(s, mid) == []   # код не создан


@pytest.mark.asyncio
async def test_fr_promo_requires_limit(db):
    mid = await _merchant(db, earnings="1000")
    h = _flow()
    st = _State()
    await st.update_data(p_amount="100")
    msg = _Msg("0")  # без лимита — франшизе нельзя
    await h["promo_limit"](msg, db, st, merchant_id=mid)
    assert any("лимит" in a.lower() for a in msg.answers)
    async with db.session() as s:
        assert await bn.list_promos(s, mid) == []


@pytest.mark.asyncio
async def test_fr_promo_confirm_creates(db):
    mid = await _merchant(db, earnings="500")
    h = _flow()
    st = _State()
    await st.update_data(p_amount="100")
    msg = _Msg("2")  # нужно 200 ≤ 500 → предупреждение
    await h["promo_limit"](msg, db, st, merchant_id=mid)
    assert any("Остаток" in a for a in msg.answers)       # предупреждение с остатком
    call = _Call("mbn:pok", _Bot())
    await h["promo_confirm"](call, db, st, merchant_id=mid)
    async with db.session() as s:
        promos = await bn.list_promos(s, mid)
    assert len(promos) == 1 and promos[0].max_activations == 2


@pytest.mark.asyncio
async def test_fr_drop_send_blocked_when_low_balance(db):
    mid = await _merchant(db, earnings="100")
    h = _flow()
    st = _State()
    await st.update_data(d_amount="50", d_limit=5)  # нужно 250 > 100
    call = _Call("mbn:dsend", _Bot())
    await h["drop_send"](call, db, st, merchant_id=mid)
    assert any("Недостаточно" in o for o in call.message.outs)
    # кнопки «разослать» нет → go тоже защищён
    call2 = _Call("mbn:dgo", _Bot())
    await h["drop_go"](call2, db, st, merchant_id=mid)
    assert any("Недостаточно" in o for o in call2.message.outs)
    async with db.session() as s:
        assert await bn.recent_drops(s, mid) == []


@pytest.mark.asyncio
async def test_fr_drop_send_shows_warning(db):
    mid = await _merchant(db, earnings="500")
    h = _flow()
    st = _State()
    await st.update_data(d_amount="50", d_limit=5)  # нужно 250 ≤ 500
    call = _Call("mbn:dsend", _Bot())
    await h["drop_send"](call, db, st, merchant_id=mid)
    joined = " ".join(call.message.outs)
    assert "Остаток" in joined and "максимум" in joined

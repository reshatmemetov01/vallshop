"""Посты в канал: клавиатура (cpk), счётчик кликов и визард публикации."""

import pytest
from aiogram import Router
from aiogram.fsm.state import State, StatesGroup

from bot.db import Database, User
from bot.db.models import Broadcast, BroadcastClick
from bot.services import channel as ch
from bot.services import broadcasts as bn
from bot.handlers import channel_post, channel_user, broadcast_flow
from bot import keyboards as kb


@pytest.fixture(autouse=True)
def _reset():
    ch._cfg.clear(); ch._sub.clear()
    yield
    ch._cfg.clear(); ch._sub.clear()


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


# ── Клавиатура поста ──────────────────────────────────────────────────────────
def test_channel_post_kb():
    btns = [
        {"label": "Каталог", "kind": "cb", "target": "catalog", "style": "primary", "icon": "1"},
        {"label": "Сайт", "kind": "url", "target": "@chan", "style": None, "icon": None},
    ]
    m = kb.channel_post_kb(btns, post_id=5)
    flat = [b for row in m.inline_keyboard for b in row]
    assert flat[0].callback_data == "cpk:5:0:catalog"       # раздел → трекинг-колбэк
    assert flat[1].url == "https://t.me/chan"               # ссылка → обычный url
    assert flat[1].callback_data is None


# ── Клик по кнопке поста ──────────────────────────────────────────────────────
class _Me:
    username = "mybot"


class _Bot:
    async def me(self):
        return _Me()


class _Call:
    def __init__(self, data):
        self.data = data
        self.bot = _Bot()

        class _U:
            id = 77
        self.from_user = _U()
        self.answered = []

    async def answer(self, text=None, url=None, show_alert=False):
        self.answered.append(url or text)


@pytest.mark.asyncio
async def test_cpk_click_records_and_opens_bot(db):
    async with db.session() as s:
        await bn.create(s, merchant_id=0, created_by=1, text="t", photo=None,
                        buttons=[{"label": "A", "kind": "cb", "target": "catalog"}],
                        status="channel")
    call = _Call("cpk:1:0:catalog")
    await channel_user.cb_channel_post_click(call, db, merchant_id=0)
    # клик записан
    async with db.session() as s:
        row = await s.get(BroadcastClick, {"broadcast_id": 1, "btn_idx": 0, "user_id": 77})
        assert row is not None
    # открыли бота по deep-link
    assert any("t.me/mybot?start=" in (a or "") for a in call.answered)


# ── Визард публикации ─────────────────────────────────────────────────────────
class _S(StatesGroup):
    compose = State(); url = State(); label = State(); emoji = State()


class _State:
    def __init__(self):
        self._d = {}

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **k):
        self._d.update(k); return dict(self._d)

    async def set_state(self, s):
        pass

    async def clear(self):
        self._d = {}


class _ChanBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, reply_markup=None):
        self.sent.append((chat, reply_markup))

    async def send_photo(self, chat, photo, caption=None, reply_markup=None):
        self.sent.append((chat, reply_markup))


class _CMsg:
    def __init__(self, bot):
        self.bot = bot

    async def edit_text(self, t, reply_markup=None):
        pass

    async def answer(self, t, reply_markup=None):
        pass

    async def answer_photo(self, p, caption=None, reply_markup=None):
        pass


class _WCall:
    def __init__(self, data, bot):
        self.data = data
        self.message = _CMsg(bot)

        class _U:
            id = 9
        self.from_user = _U()
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


class _Msg:
    def __init__(self, text):
        self.text = text; self.caption = None; self.photo = None

        class _U:
            id = 9
        self.from_user = _U()
        self.answers = []

    @property
    def html_text(self):
        return self.text

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_publish_wizard_sends_to_channel(db):
    h = channel_post.register(Router(), prefix="hpost", home_cb="ch:home",
                              sections=broadcast_flow.HEAD_SECTIONS, states=_S)
    await ch.set_chat(db, 0, "@mychan")
    bot = _ChanBot()
    st = _State()
    await h["new"](_WCall("hpost:new", bot), st)
    await h["compose"](_Msg("Привет, канал!"), st)
    await h["sp"](_WCall("hpost:sp:catalog", bot), st)      # кнопка-раздел
    await h["send"](_WCall("hpost:send", bot), st, merchant_id=0)
    await h["go"](_WCall("hpost:go", bot), db, st, merchant_id=0)

    # пост ушёл в канал с cpk-кнопкой
    assert len(bot.sent) == 1
    chat, markup = bot.sent[0]
    assert chat == "@mychan"
    assert markup.inline_keyboard[0][0].callback_data.startswith("cpk:")
    # запись поста создана со статусом channel
    async with db.session() as s:
        posts = await bn.channel_posts(s, 0)
    assert len(posts) == 1 and posts[0].status == "channel"


@pytest.mark.asyncio
async def test_publish_blocked_without_channel(db):
    h = channel_post.register(Router(), prefix="hpost", home_cb="ch:home",
                              sections=broadcast_flow.HEAD_SECTIONS, states=_S)
    bot = _ChanBot()
    st = _State()
    await st.update_data(d_text="Привет", d_btns=[])
    call = _WCall("hpost:send", bot)
    await h["send"](call, st, merchant_id=0)        # канал не привязан
    await h["go"](_WCall("hpost:go", bot), db, st, merchant_id=0)
    assert bot.sent == []                            # ничего не опубликовано

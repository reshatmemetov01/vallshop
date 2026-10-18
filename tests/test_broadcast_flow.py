"""Движок рассылки (broadcast_flow): черновик, конструктор кнопок, отправка.

Хендлеры вызываем напрямую (register возвращает их map), подставляя фейковые
call/message/state. Проверяем, что черновик и кнопки корректно копятся в FSM,
а при отправке строится пост-клавиатура и обновляется достижимость.
"""

import pytest
from aiogram import Router
from aiogram.fsm.state import State, StatesGroup

from bot.db import Database, User
from bot.handlers import broadcast_flow as bf


class _States(StatesGroup):
    compose = State()
    url = State()
    label = State()
    emoji = State()


class _FakeState:
    def __init__(self):
        self._data = {}
        self._state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)
        return dict(self._data)

    async def set_state(self, st):
        self._state = st

    async def clear(self):
        self._data = {}
        self._state = None


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, reply_markup=None):
        self.sent.append((uid, text, reply_markup))

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self.sent.append((uid, photo, caption, reply_markup))


class _FakeMsg:
    def __init__(self, text=None, photo=None, caption=None, entities=None):
        self.text = text
        self.caption = caption
        self.photo = photo
        self.entities = entities or []
        self.bot = _FakeBot()
        self.answers = []

    @property
    def html_text(self):
        return self.text or self.caption

    async def edit_text(self, text, reply_markup=None):
        self.answers.append(("edit", text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.answers.append(("answer", text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.answers.append(("photo", photo, caption, reply_markup))


class _FakeUser:
    id = 555


class _FakeCall:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = _FakeUser()
        self.answered = []

    async def answer(self, text=None, show_alert=False):
        self.answered.append(text)


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    async with database.session() as s:
        s.add(User(id=1, merchant_id=0, is_active=True))
        s.add(User(id=2, merchant_id=0, is_active=True))
        await s.commit()
    yield database
    await database.dispose()


@pytest.fixture
def handlers():
    return bf.register(
        Router(), prefix="hb", home_cb="st_home", title="Рассылка",
        sections=bf.HEAD_SECTIONS, states=_States,
    )


@pytest.mark.asyncio
async def test_compose_then_add_section_builds_draft(handlers, db, monkeypatch):
    monkeypatch.setattr(bf.broadcast_service, "_DELAY", 0)
    st = _FakeState()

    # 1) composer: прислали текст
    msg = _FakeMsg(text="Привет, <b>акция</b>!")
    await handlers["compose"](msg, st)
    assert (await st.get_data())["d_text"] == "Привет, <b>акция</b>!"

    # 2) добавляем кнопку-раздел «Каталог»
    call = _FakeCall("hb:sp:catalog", _FakeMsg())
    await handlers["section_pick"](call, st)
    btns = (await st.get_data())["d_btns"]
    assert len(btns) == 1
    assert btns[0]["kind"] == "cb" and btns[0]["target"] == "catalog"
    assert btns[0]["style"] == "primary" and btns[0]["icon"]  # есть премиум-эмодзи

    # 3) меняем цвет на красный
    call = _FakeCall("hb:bs:0:danger", _FakeMsg())
    await handlers["setcolor"](call, st)
    assert (await st.get_data())["d_btns"][0]["style"] == "danger"

    # 4) отправка
    call = _FakeCall("hb:go", _FakeMsg())
    await handlers["go"](call, db, st, merchant_id=0)
    # фоновая задача _job создана; дождёмся её через цикл событий
    import asyncio
    await asyncio.sleep(0.05)
    # пост ушёл обоим клиентам, с клавиатурой
    bot = call.message.bot
    # _job использует call.message.bot — тот же объект
    assert len(bot.sent) >= 2
    _, _, markup = bot.sent[0]
    assert markup is not None and markup.inline_keyboard[0][0].callback_data == "catalog"


@pytest.mark.asyncio
async def test_url_button_validation(handlers):
    st = _FakeState()
    # мусор вместо ссылки — кнопка не создаётся
    bad = _FakeMsg(text="это не ссылка потому что пробелы")
    await handlers["url"](bad, st)
    assert "d_btns" not in (await st.get_data()) or not (await st.get_data())["d_btns"]
    # нормальная ссылка — кнопка создаётся, @username нормализуется
    ok = _FakeMsg(text="@mychannel")
    await handlers["url"](ok, st)
    btns = (await st.get_data())["d_btns"]
    assert btns[0]["kind"] == "url" and btns[0]["target"] == "https://t.me/mychannel"


@pytest.mark.asyncio
async def test_button_limit_blocks_sixth(handlers):
    st = _FakeState()
    await st.update_data(d_btns=[{"label": f"b{i}", "kind": "cb", "target": "menu"}
                                 for i in range(bf.MAX_BUTTONS)])
    call = _FakeCall("hb:sp:catalog", _FakeMsg())
    await handlers["section_pick"](call, st)
    assert len((await st.get_data())["d_btns"]) == bf.MAX_BUTTONS  # не добавилась
    assert call.answered and "лимит" in call.answered[-1].lower()

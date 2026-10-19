"""Обязательная подписка: настройки/проверка, парсер ссылки, гейт-middleware."""

import pytest

from bot.db import Database, User
from bot.services import channel as ch
from bot.handlers import channel_flow
from bot.middlewares import ChannelGateMiddleware


@pytest.fixture(autouse=True)
def _reset_caches():
    ch._cfg.clear()
    ch._sub.clear()
    yield
    ch._cfg.clear()
    ch._sub.clear()


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


# ── Парсер ссылки ─────────────────────────────────────────────────────────────
def test_parse_link():
    assert channel_flow._parse_link("@mychan") == ("https://t.me/mychan", "@mychan")
    assert channel_flow._parse_link("t.me/mychan") == ("https://t.me/mychan", "@mychan")
    url, chat = channel_flow._parse_link("https://t.me/+AbCdEf123")
    assert url == "https://t.me/+AbCdEf123" and chat is None   # приватная — только ссылка
    assert channel_flow._parse_link("не ссылка вовсе") == (None, None)


# ── Настройки + кэш ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_set_and_config(db):
    await ch.set_url(db, 0, "https://t.me/x")
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)
    cfg = ch.config(0)
    assert cfg == {"url": "https://t.me/x", "chat": "@x", "required": True}
    await ch.clear(db, 0)
    assert ch.config(0) == {"url": None, "chat": None, "required": False}


# ── Проверка подписки ─────────────────────────────────────────────────────────
class _Member:
    def __init__(self, status):
        self.status = status


class _Bot:
    id = 7

    def __init__(self, status=None, raise_=False):
        self._status = status
        self._raise = raise_

    async def get_chat_member(self, chat, user_id):
        if self._raise:
            raise RuntimeError("bot is not a member of the channel chat")
        return _Member(self._status)


@pytest.mark.asyncio
async def test_is_subscribed_statuses(db):
    await ch.set_chat(db, 0, "@x")
    assert await ch.is_subscribed(_Bot("member"), 0, 1) is True
    ch._sub.clear()
    assert await ch.is_subscribed(_Bot("left"), 0, 1) is False
    ch._sub.clear()
    # проверить нельзя → None (fail-open)
    assert await ch.is_subscribed(_Bot(raise_=True), 0, 1) is None


@pytest.mark.asyncio
async def test_is_subscribed_no_chat_is_none(db):
    assert await ch.is_subscribed(_Bot("member"), 0, 1) is None   # канал не привязан


@pytest.mark.asyncio
async def test_subscribed_cached(db):
    await ch.set_chat(db, 0, "@x")
    assert await ch.is_subscribed(_Bot("member"), 0, 1) is True
    # из кэша: даже если сейчас проверка упала бы — вернёт True (в пределах TTL)
    assert await ch.is_subscribed(_Bot(raise_=True), 0, 1) is True
    # force — свежая проверка, минуя кэш
    assert await ch.is_subscribed(_Bot(raise_=True), 0, 1, force=True) is None


# ── Гейт-middleware ───────────────────────────────────────────────────────────
class _Cfg:
    admin_ids = [999]

    def is_admin(self, uid):
        return uid in self.admin_ids


class _U:
    def __init__(self, i):
        self.id = i


class _Msg:
    def __init__(self):
        self.answers = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


async def _run_gate(db, bot, user_id, event=None):
    mw = ChannelGateMiddleware()
    called = {"v": False}

    async def handler(ev, data):
        called["v"] = True

    ev = event if event is not None else _Msg()
    ev_user = _U(user_id)
    data = {"event_from_user": ev_user, "bot": bot, "config": _Cfg(),
            "db": db, "merchant_id": 0}
    await mw(handler, ev, data)
    return called["v"], ev


@pytest.mark.asyncio
async def test_gate_blocks_unsubscribed(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)
    called, ev = await _run_gate(db, _Bot("left"), user_id=1)
    assert called is False                       # действие заблокировано
    assert ev.answers and "подпиш" in ev.answers[0].lower()


@pytest.mark.asyncio
async def test_gate_allows_subscribed(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)
    called, _ = await _run_gate(db, _Bot("member"), user_id=1)
    assert called is True


@pytest.mark.asyncio
async def test_gate_exempts_admin(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)
    called, _ = await _run_gate(db, _Bot("left"), user_id=999)   # админ
    assert called is True


@pytest.mark.asyncio
async def test_gate_off_when_not_required(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, False)
    called, _ = await _run_gate(db, _Bot("left"), user_id=1)
    assert called is True                        # гейт выключен — пускаем


@pytest.mark.asyncio
async def test_gate_fail_open_when_cannot_check(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)
    called, _ = await _run_gate(db, _Bot(raise_=True), user_id=1)
    assert called is True                        # проверить нельзя → пускаем


@pytest.mark.asyncio
async def test_gate_lets_chksub_through(db):
    await ch.set_chat(db, 0, "@x")
    await ch.set_required(db, 0, True)

    class _Cb:
        data = "chksub"
        message = _Msg()

        async def answer(self, *a, **k):
            pass

    called, _ = await _run_gate(db, _Bot("left"), user_id=1, event=_Cb())
    assert called is True                        # кнопка «Проверить» доходит до обработчика

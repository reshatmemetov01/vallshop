"""Рассылка: получатели по мерчанту, подсчёт отправок, пометка активных/неактивных."""

import pytest

from bot.db import Database, User
from bot.services import broadcast as bc
from bot.services import stats as stats_service
from bot import keyboards as kb


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_recipients_scoped_to_merchant(db):
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, username="head"))
        s.add(User(id=2, merchant_id=1, username="fr1a"))
        s.add(User(id=3, merchant_id=1, username="fr1b"))
        s.add(User(id=4, merchant_id=1, is_blocked=True))   # забанен — пропустить
        s.add(User(id=5, merchant_id=2, username="fr2"))     # другая франшиза
        await s.commit()
    assert sorted(await bc.recipients(db, 1)) == [2, 3]       # только клиенты франшизы 1
    assert await bc.recipients(db, 0) == [1]                  # головной отдельно
    assert await bc.recipients(db, 2) == [5]


class _FakeBot:
    def __init__(self, fail_ids=(), block_ids=()):
        self.sent = []
        self.fail_ids = set(fail_ids)
        self.block_ids = set(block_ids)

    def _maybe_raise(self, uid):
        if uid in self.block_ids:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        if uid in self.fail_ids:
            raise RuntimeError("some transient error")

    async def send_message(self, uid, text, reply_markup=None):
        self._maybe_raise(uid)
        self.sent.append((uid, text, reply_markup))

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self._maybe_raise(uid)
        self.sent.append((uid, photo, caption, reply_markup))


@pytest.mark.asyncio
async def test_run_counts_and_detects_blocked(monkeypatch):
    monkeypatch.setattr(bc, "_DELAY", 0)
    bot = _FakeBot(fail_ids={3}, block_ids={4})
    sent, failed, ok_ids, blocked_ids = await bc.run(bot, [1, 2, 3, 4], text="Привет")
    assert sent == 2 and failed == 2
    assert sorted(ok_ids) == [1, 2]
    assert blocked_ids == [4]            # только «заблокировал бота», не транзиентная 3
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_run_passes_reply_markup(monkeypatch):
    monkeypatch.setattr(bc, "_DELAY", 0)
    bot = _FakeBot()
    markup = kb.broadcast_post_kb([{"label": "Каталог", "kind": "cb", "target": "catalog"}])
    await bc.run(bot, [1], text="Hi", reply_markup=markup)
    assert bot.sent[0][2] is markup


@pytest.mark.asyncio
async def test_apply_delivery_marks_active_inactive(db):
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, is_active=True))
        s.add(User(id=2, merchant_id=0, is_active=False))  # был неактивен → вернулся
        s.add(User(id=3, merchant_id=0, is_active=True))   # заблокировал бота
        await s.commit()
    await bc.apply_delivery(db, 0, ok_ids=[1, 2], blocked_ids=[3])
    async with db.session() as s:
        assert (await s.get(User, {"id": 1, "merchant_id": 0})).is_active is True
        assert (await s.get(User, {"id": 2, "merchant_id": 0})).is_active is True
        assert (await s.get(User, {"id": 3, "merchant_id": 0})).is_active is False


@pytest.mark.asyncio
async def test_apply_delivery_scoped_to_merchant(db):
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, is_active=True))
        s.add(User(id=1, merchant_id=1, is_active=True))  # тот же tg-id, другая франшиза
        await s.commit()
    await bc.apply_delivery(db, 1, ok_ids=[], blocked_ids=[1])
    async with db.session() as s:
        assert (await s.get(User, {"id": 1, "merchant_id": 0})).is_active is True   # не тронут
        assert (await s.get(User, {"id": 1, "merchant_id": 1})).is_active is False


@pytest.mark.asyncio
async def test_activity_stats(db):
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, is_active=True))
        s.add(User(id=2, merchant_id=0, is_active=False))
        s.add(User(id=3, merchant_id=0, is_active=True, is_blocked=True))  # забанен — вне базы
        s.add(User(id=4, merchant_id=1, is_active=True))
        await s.commit()
        act = await stats_service.activity_stats(s, 0)
    assert act == {"total": 2, "active": 1, "inactive": 1}

"""Рассылка франшизы: получатели в разрезе мерчанта + подсчёт отправок."""

import pytest

from bot.db import Database, User
from bot.services import broadcast as bc


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
        s.add(User(id=4, merchant_id=1, is_blocked=True))   # заблокирован — пропустить
        s.add(User(id=5, merchant_id=2, username="fr2"))     # другая франшиза
        await s.commit()
    assert sorted(await bc.recipients(db, 1)) == [2, 3]       # только клиенты франшизы 1
    assert await bc.recipients(db, 0) == [1]                  # головной отдельно
    assert await bc.recipients(db, 2) == [5]


class _FakeBot:
    def __init__(self, fail_ids=()):
        self.sent = []
        self.fail_ids = set(fail_ids)

    async def send_message(self, uid, text):
        if uid in self.fail_ids:
            raise RuntimeError("blocked")
        self.sent.append((uid, text))

    async def send_photo(self, uid, photo, caption=None):
        if uid in self.fail_ids:
            raise RuntimeError("blocked")
        self.sent.append((uid, photo, caption))


@pytest.mark.asyncio
async def test_run_counts_sent_and_failed(monkeypatch):
    # не ждём реальных пауз
    import bot.services.broadcast as mod
    monkeypatch.setattr(mod, "_DELAY", 0)
    bot = _FakeBot(fail_ids={3})
    sent, failed = await bc.run(bot, [1, 2, 3], text="Привет")
    assert sent == 2 and failed == 1
    assert len(bot.sent) == 2

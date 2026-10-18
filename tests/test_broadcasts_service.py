"""Сервис рассылок: запись, расписание (claim_due), отмена, клики, доставка."""

from datetime import datetime, timedelta

import pytest

from bot.db import Database, User
from bot.db.models import Broadcast
from bot.services import broadcast as bc_svc
from bot.services import broadcasts as svc
from bot import keyboards as kb


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.mark.asyncio
async def test_create_and_buttons_roundtrip(db):
    async with db.session() as s:
        bc = await svc.create(
            s, merchant_id=0, created_by=99, text="hi", photo=None,
            buttons=[{"label": "Каталог", "kind": "cb", "target": "catalog"}],
            status="scheduled", scheduled_at=datetime(2030, 1, 1, 12, 0),
        )
    assert bc.id is not None
    async with db.session() as s:
        got = await svc.get(s, bc.id, merchant_id=0)
        assert got is not None
        assert svc.buttons_of(got)[0]["target"] == "catalog"
        # чужой мерчант не видит
        assert await svc.get(s, bc.id, merchant_id=1) is None


@pytest.mark.asyncio
async def test_claim_due_only_ripe_and_once(db):
    now = datetime.utcnow()
    async with db.session() as s:
        await svc.create(s, merchant_id=0, created_by=1, text="past", photo=None,
                         buttons=[], status="scheduled", scheduled_at=now - timedelta(minutes=1))
        await svc.create(s, merchant_id=0, created_by=1, text="future", photo=None,
                         buttons=[], status="scheduled", scheduled_at=now + timedelta(hours=1))
    first = await svc.claim_due(db, now)
    assert len(first) == 1 and first[0][1] == 0   # только созревшая, merchant_id=0
    second = await svc.claim_due(db, now)
    assert second == []                            # повторно не забираем (уже sending)


@pytest.mark.asyncio
async def test_cancel_only_scheduled_and_own(db):
    async with db.session() as s:
        bc = await svc.create(s, merchant_id=1, created_by=1, text="x", photo=None,
                              buttons=[], status="scheduled",
                              scheduled_at=datetime.utcnow() + timedelta(hours=1))
    assert await svc.cancel(db, bc.id, merchant_id=0) is False   # чужой — нельзя
    assert await svc.cancel(db, bc.id, merchant_id=1) is True    # свой — ок
    assert await svc.cancel(db, bc.id, merchant_id=1) is False   # уже отменён


@pytest.mark.asyncio
async def test_record_click_and_button_stats(db):
    async with db.session() as s:
        bc = await svc.create(s, merchant_id=0, created_by=1, text="x", photo=None,
                              buttons=[{"label": "A", "kind": "cb", "target": "catalog"}],
                              status="done")
    bid = bc.id
    await svc.record_click(db, bid, 0, user_id=100)
    await svc.record_click(db, bid, 0, user_id=100)   # тот же — total++ , uniq тот же
    await svc.record_click(db, bid, 0, user_id=200)   # новый уникальный
    async with db.session() as s:
        st = await svc.button_stats(s, bid)
    assert st[0] == {"uniq": 2, "total": 3}


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, reply_markup=None):
        self.sent.append((uid, reply_markup))

    async def send_photo(self, uid, photo, caption=None, reply_markup=None):
        self.sent.append((uid, reply_markup))


@pytest.mark.asyncio
async def test_deliver_sends_and_marks_done(db, monkeypatch):
    monkeypatch.setattr(bc_svc, "_DELAY", 0)
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, is_active=True))
        s.add(User(id=2, merchant_id=0, is_active=True))
        bc = await svc.create(s, merchant_id=0, created_by=1, text="hi", photo=None,
                              buttons=[{"label": "A", "kind": "cb", "target": "catalog"}],
                              status="sending")
    bot = _FakeBot()
    sent, failed = await svc.deliver(bot, db, bc.id)
    assert (sent, failed) == (2, 0)
    # пост с tracking-кнопкой bck:<id>:0:catalog
    _, markup = bot.sent[0]
    assert markup.inline_keyboard[0][0].callback_data == f"bck:{bc.id}:0:catalog"
    async with db.session() as s:
        obj = await s.get(Broadcast, bc.id)
        assert obj.status == "done" and obj.sent == 2 and obj.recipients == 2
        assert obj.sent_at is not None

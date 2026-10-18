"""Уведомления админам о пополнении баланса (головной + владелец франшизы)."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant, User
from bot.services import notify as notify_service


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


class _Cfg:
    def __init__(self, admin_ids):
        self.admin_ids = admin_ids


class _Bot:
    def __init__(self):
        self.sent = []  # (chat_id, text)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_head_topup_notifies_only_head(db):
    async with db.session() as s:
        s.add(User(id=100, merchant_id=0, username="buyer", balance=Decimal("500")))
        await s.commit()
    head = _Bot()
    await notify_service.notify_topup(
        db, _Cfg([1, 2]), {0: head}, user_id=100, amount=Decimal("500"), merchant_id=0
    )
    assert len(head.sent) == 2                       # оба головных админа
    assert all("@buyer" in t and "500" in t for _, t in head.sent)
    assert {cid for cid, _ in head.sent} == {1, 2}


@pytest.mark.asyncio
async def test_franchise_topup_notifies_head_and_owner(db):
    async with db.session() as s:
        m = Merchant(title="Магаз Пети", bot_token="t", bot_username="f", admin_user_id=50)
        s.add(m)
        await s.flush()
        fid = m.id
        s.add(User(id=100, merchant_id=fid, username="buyer", balance=Decimal("300")))
        await s.commit()
    head, fbot = _Bot(), _Bot()
    await notify_service.notify_topup(
        db, _Cfg([1]), {0: head, fid: fbot}, user_id=100, amount=Decimal("300"), merchant_id=fid
    )
    # головному — с названием франшизы
    assert len(head.sent) == 1 and "Магаз Пети" in head.sent[0][1]
    # владельцу франшизы (id 50)
    assert len(fbot.sent) == 1 and fbot.sent[0][0] == 50


@pytest.mark.asyncio
async def test_self_topup_not_notified(db):
    """Админ пополнил свой баланс — себя не уведомляем."""
    async with db.session() as s:
        s.add(User(id=1, merchant_id=0, username="owner", balance=Decimal("100")))
        await s.commit()
    head = _Bot()
    await notify_service.notify_topup(
        db, _Cfg([1, 2]), {0: head}, user_id=1, amount=Decimal("100"), merchant_id=0
    )
    assert {cid for cid, _ in head.sent} == {2}       # админ 1 (сам) пропущен


@pytest.mark.asyncio
async def test_franchise_owner_self_topup_skipped(db):
    async with db.session() as s:
        m = Merchant(title="F", bot_token="t", bot_username="f", admin_user_id=50)
        s.add(m)
        await s.flush()
        fid = m.id
        s.add(User(id=50, merchant_id=fid, username="petya", balance=Decimal("10")))
        await s.commit()
    head, fbot = _Bot(), _Bot()
    await notify_service.notify_topup(
        db, _Cfg([1]), {0: head, fid: fbot}, user_id=50, amount=Decimal("10"), merchant_id=fid
    )
    assert len(head.sent) == 1                         # головному всё равно приходит
    assert fbot.sent == []                             # владельцу-самому — нет

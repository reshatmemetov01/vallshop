"""Бонусы: промокоды (лимит, 1 раз/юзер, оплата франшизой) и дропы (клик-лимит)."""

from decimal import Decimal

import pytest

from bot.db import Database, Merchant, User
from bot.services import balance as balance_service
from bot.services import bonuses as bn


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    yield database
    await database.dispose()


async def _users(db, ids, merchant_id=0):
    async with db.session() as s:
        for i in ids:
            s.add(User(id=i, merchant_id=merchant_id))
        await s.commit()


async def _merchant(db, earnings="0"):
    async with db.session() as s:
        m = Merchant(title="F", bot_token="t", bot_username="f", admin_user_id=1,
                     earnings_rub=Decimal(earnings), earnings_total_rub=Decimal(earnings))
        s.add(m)
        await s.commit()
        return m.id


# ── Промокоды ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_promo_redeem_limit_and_once_per_user(db):
    await _users(db, [1, 2, 3])
    async with db.session() as s:
        promo = await bn.create_promo(s, merchant_id=0, created_by=9,
                                      amount_rub=Decimal("100"), max_activations=2)
        await s.commit()
        code = promo.code
    assert (await bn.redeem(db, 0, 1, code)) == ("ok", Decimal("100"))
    async with db.session() as s:
        assert await balance_service.get_balance(s, 1, 0) == Decimal("100")
    # тот же юзер второй раз — нельзя
    assert (await bn.redeem(db, 0, 1, code))[0] == "already"
    # второй юзер — ок (лимит 2)
    assert (await bn.redeem(db, 0, 2, code))[0] == "ok"
    # третий — лимит исчерпан
    assert (await bn.redeem(db, 0, 3, code))[0] == "exhausted"


@pytest.mark.asyncio
async def test_promo_not_found_and_inactive(db):
    await _users(db, [1])
    async with db.session() as s:
        promo = await bn.create_promo(s, merchant_id=0, created_by=9,
                                      amount_rub=Decimal("50"), max_activations=0)
        await s.commit()
        code = promo.code
        pid = promo.id
    assert (await bn.redeem(db, 0, 1, "НЕТТАКОГО"))[0] == "not_found"
    await bn.set_active(db, pid, 0, False)
    assert (await bn.redeem(db, 0, 1, code))[0] == "not_found"


@pytest.mark.asyncio
async def test_promo_franchise_deducts_earnings(db):
    mid = await _merchant(db, earnings="150")
    await _users(db, [1, 2], merchant_id=mid)
    async with db.session() as s:
        promo = await bn.create_promo(s, merchant_id=mid, created_by=1,
                                      amount_rub=Decimal("100"), max_activations=0)
        await s.commit()
        code = promo.code
        pid = promo.id
    assert (await bn.redeem(db, mid, 1, code))[0] == "ok"
    async with db.session() as s:
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("50")  # 150-100
    # второму не хватает заработка франшизы (50 < 100)
    assert (await bn.redeem(db, mid, 2, code))[0] == "nofunds"
    async with db.session() as s:
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("50")  # не ушло в минус
        assert await balance_service.get_balance(s, 2, mid) == Decimal("0")
        assert (await s.get(type(promo), pid)).used_count == 1  # откат инкремента


# ── Бонус-дропы ──────────────────────────────────────────────────────────────
async def _drop(db, merchant_id=0, amount="50", limit=2):
    async with db.session() as s:
        d = await bn.create_drop(
            s, merchant_id=merchant_id, created_by=9, text="t", photo=None,
            btn_label="go", amount_rub=Decimal(amount), max_claims=limit,
            success_text="ok", error_text="no",
            success_btn_label=None, success_btn_kind=None, success_btn_target=None,
        )
    await bn.activate_drop(db, d.id, 3)
    return d.id


@pytest.mark.asyncio
async def test_drop_claim_limit_and_once(db):
    await _users(db, [1, 2, 3])
    did = await _drop(db, limit=2)
    st, info = await bn.claim(db, 0, did, 1)
    assert st == "ok" and info["amount"] == Decimal("50")
    async with db.session() as s:
        assert await balance_service.get_balance(s, 1, 0) == Decimal("50")
    assert (await bn.claim(db, 0, did, 1))[0] == "already"
    assert (await bn.claim(db, 0, did, 2))[0] == "ok"
    assert (await bn.claim(db, 0, did, 3))[0] == "ended"   # лимит 2 исчерпан


@pytest.mark.asyncio
async def test_drop_claim_franchise_funds(db):
    mid = await _merchant(db, earnings="50")
    await _users(db, [1, 2], merchant_id=mid)
    did = await _drop(db, merchant_id=mid, amount="50", limit=5)
    assert (await bn.claim(db, mid, did, 1))[0] == "ok"
    async with db.session() as s:
        assert (await s.get(Merchant, mid)).earnings_rub == Decimal("0")
    # средств франшизы больше нет → считаем, что бонусы кончились
    assert (await bn.claim(db, mid, did, 2))[0] == "ended"
    async with db.session() as s:
        assert await balance_service.get_balance(s, 2, mid) == Decimal("0")


@pytest.mark.asyncio
async def test_drop_claim_wrong_merchant(db):
    await _users(db, [1])
    did = await _drop(db, merchant_id=0)
    # другой магазин не видит чужой дроп
    assert (await bn.claim(db, 7, did, 1))[0] == "not_found"


@pytest.mark.asyncio
async def test_drop_claimable_right_after_create(db):
    """Регрессия: забрать можно сразу после создания, не дожидаясь конца рассылки."""
    await _users(db, [1])
    async with db.session() as s:
        d = await bn.create_drop(
            s, merchant_id=0, created_by=9, text="t", photo=None, btn_label="go",
            amount_rub=Decimal("50"), max_claims=2, success_text="ok", error_text="no",
            success_btn_label=None, success_btn_kind=None, success_btn_target=None,
        )
        assert d.status == "active" and d.expires_at is not None
    # activate_drop НЕ вызываем — имитируем «рассылка ещё идёт / прервалась»
    st, info = await bn.claim(db, 0, d.id, 1)
    assert st == "ok" and info["amount"] == Decimal("50")


class _DelBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


@pytest.mark.asyncio
async def test_purge_drop_deletes_messages_and_stops_claims(db):
    await _users(db, [1])
    did = await _drop(db, limit=2)                 # активная рассылка
    await bn.store_message(db, did, 100, 11)       # сохранённые сообщения (пост/служебные)
    await bn.store_message(db, did, 101, 12)
    bot = _DelBot()
    n = await bn.purge_drop(bot, db, did)
    assert n == 2 and len(bot.deleted) == 2        # удалены у получателей
    # активации остановлены
    assert (await bn.claim(db, 0, did, 1))[0] == "ended"
    # сообщения помечены удалёнными — повторно не трогаем
    async with db.session() as s:
        assert await bn.messages_to_delete(s, did) == []

"""Бонусы: промокоды на баланс и бонус-рассылки (дропы).

Начисление идёт на реальный баланс (``balance.credit``). У франшизы сумма бонуса
списывается с её заработка (``Merchant.earnings_rub``) — она платит за свои акции;
не хватает заработка → бонус не выдаётся. У головного (merchant_id=0) списания нет.

Лимиты активаций защищены атомарным UPDATE с условием (без гонок при одновременных
нажатиях). Один пользователь получает бонус из кода/дропа только один раз.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import Database
from ..db.models import (
    HEAD_MERCHANT_ID,
    BonusActivation,
    BonusDrop,
    BonusDropMessage,
    Merchant,
    PromoActivation,
    PromoCode,
)
from . import balance as balance_service
from . import broadcast as broadcast_service
from .. import keyboards as kb

log = logging.getLogger("vallshop.bonuses")

DROP_TTL_HOURS = 24
_SEND_DELAY = 0.05
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O, 1/I


def normalize_code(raw: str) -> str:
    return (raw or "").strip().upper().replace(" ", "")


def gen_code(length: int = 8) -> str:
    return "".join(random.choice(_CODE_ALPHABET) for _ in range(length))


# ── Франшиза платит за свои бонусы из заработка ──────────────────────────────
async def _deduct_earnings(session: AsyncSession, merchant_id: int, amount: Decimal) -> bool:
    """Атомарно списать сумму с заработка франшизы. Головной — всегда True."""
    if merchant_id == HEAD_MERCHANT_ID:
        return True
    res = await session.execute(
        update(Merchant)
        .where(Merchant.id == merchant_id, Merchant.earnings_rub >= Decimal(amount))
        .values(earnings_rub=Merchant.earnings_rub - Decimal(amount))
    )
    await session.flush()
    return res.rowcount == 1


# ── Промокоды ────────────────────────────────────────────────────────────────
async def create_promo(
    session: AsyncSession, *, merchant_id: int, created_by: int,
    amount_rub: Decimal, max_activations: int = 0, code: Optional[str] = None,
) -> PromoCode:
    """Создать промокод (уникальный в пределах магазина)."""
    for _ in range(20):
        c = normalize_code(code) if code else gen_code()
        exists = await session.scalar(
            select(PromoCode.id).where(
                PromoCode.merchant_id == merchant_id, PromoCode.code == c
            )
        )
        if not exists:
            promo = PromoCode(
                merchant_id=merchant_id, code=c, amount_rub=Decimal(amount_rub),
                max_activations=int(max_activations or 0), created_by=created_by,
            )
            session.add(promo)
            await session.flush()
            return promo
        if code:  # заданный вручную код уже есть — не перегенерируем
            raise ValueError("Такой код уже существует")
    raise RuntimeError("Не удалось сгенерировать уникальный код")


async def list_promos(session: AsyncSession, merchant_id: int, limit: int = 20) -> List[PromoCode]:
    return list(await session.scalars(
        select(PromoCode).where(PromoCode.merchant_id == merchant_id)
        .order_by(PromoCode.id.desc()).limit(limit)
    ))


async def get_promo(session: AsyncSession, promo_id: int, merchant_id: int) -> Optional[PromoCode]:
    p = await session.get(PromoCode, promo_id)
    return p if p is not None and p.merchant_id == merchant_id else None


async def set_active(db: Database, promo_id: int, merchant_id: int, active: bool) -> bool:
    async with db.session() as s:
        p = await s.get(PromoCode, promo_id)
        if p is None or p.merchant_id != merchant_id:
            return False
        p.active = active
        await s.commit()
        return True


async def redeem(
    db: Database, merchant_id: int, user_id: int, raw_code: str
) -> Tuple[str, Optional[Decimal]]:
    """Активировать промокод. Возврат (status, amount):

    status: ok | not_found | already | exhausted | nofunds.
    """
    code = normalize_code(raw_code)
    if not code:
        return "not_found", None
    async with db.session() as s:
        promo = await s.scalar(
            select(PromoCode).where(
                PromoCode.merchant_id == merchant_id, PromoCode.code == code
            )
        )
        if promo is None or not promo.active:
            return "not_found", None
        amount = Decimal(promo.amount_rub)
        # уже активировал этот код?
        if await s.get(PromoActivation, {"promo_id": promo.id, "user_id": user_id}):
            return "already", None
        # атомарно занять слот (учёт лимита)
        res = await s.execute(
            update(PromoCode)
            .where(
                PromoCode.id == promo.id,
                PromoCode.active.is_(True),
                (PromoCode.max_activations == 0)
                | (PromoCode.used_count < PromoCode.max_activations),
            )
            .values(used_count=PromoCode.used_count + 1)
        )
        if res.rowcount != 1:
            return "exhausted", None
        # франшиза платит из заработка
        if not await _deduct_earnings(s, merchant_id, amount):
            await s.rollback()
            return "nofunds", None
        await balance_service.credit(s, user_id, amount, merchant_id)
        s.add(PromoActivation(promo_id=promo.id, user_id=user_id))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return "already", None
        await s.commit()
        return "ok", amount


# ── Бонус-рассылки (дропы) ───────────────────────────────────────────────────
async def create_drop(
    session: AsyncSession, *, merchant_id: int, created_by: int,
    text: Optional[str], photo: Optional[str], btn_label: str,
    amount_rub: Decimal, max_claims: int, success_text: str, error_text: str,
    success_btn_label: Optional[str], success_btn_kind: Optional[str],
    success_btn_target: Optional[str],
) -> BonusDrop:
    drop = BonusDrop(
        merchant_id=merchant_id, created_by=created_by, text=text, photo=photo,
        btn_label=btn_label, amount_rub=Decimal(amount_rub), max_claims=int(max_claims),
        success_text=success_text, error_text=error_text,
        success_btn_label=success_btn_label, success_btn_kind=success_btn_kind,
        success_btn_target=success_btn_target,
        # Активна и с TTL сразу при создании — приём активаций открыт с момента
        # публикации, не дожидаясь конца фоновой рассылки (иначе ранние клики и
        # прерванная доставка ломали выдачу: статус застревал в «sending»).
        status="active",
        sent_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=DROP_TTL_HOURS),
    )
    session.add(drop)
    await session.flush()
    await session.commit()
    return drop


async def activate_drop(db: Database, drop_id: int, recipients: int) -> None:
    """Пометить дроп разосланным: приём активаций открыт, задать срок жизни 24ч."""
    now = datetime.utcnow()
    async with db.session() as s:
        await s.execute(
            update(BonusDrop).where(BonusDrop.id == drop_id).values(
                status="active", recipients=recipients,
                sent_at=now, expires_at=now + timedelta(hours=DROP_TTL_HOURS),
            )
        )
        await s.commit()


async def get_drop(session: AsyncSession, drop_id: int, merchant_id: Optional[int] = None):
    d = await session.get(BonusDrop, drop_id)
    if d is None:
        return None
    if merchant_id is not None and d.merchant_id != merchant_id:
        return None
    return d


async def recent_drops(session: AsyncSession, merchant_id: int, limit: int = 10) -> List[BonusDrop]:
    return list(await session.scalars(
        select(BonusDrop).where(BonusDrop.merchant_id == merchant_id)
        .order_by(BonusDrop.id.desc()).limit(limit)
    ))


async def store_message(db: Database, drop_id: int, chat_id: int, message_id: int) -> None:
    async with db.session() as s:
        s.add(BonusDropMessage(drop_id=drop_id, chat_id=chat_id, message_id=message_id))
        await s.commit()


def _claim_kb(drop_id: int, label: str):
    b = kb.InlineKeyboardBuilder()
    b.row(kb._b(label or "🎁 Забрать бонус", callback_data=f"bclaim:{drop_id}", style="success"))
    return b.as_markup()


async def deliver_drop(bot, db: Database, drop_id: int) -> int:
    """Разослать пост дропа клиентам магазина, сохранить message_id для удаления
    через 24ч и открыть приём активаций. Возвращает число отправленных."""
    async with db.session() as s:
        drop = await s.get(BonusDrop, drop_id)
    if drop is None:
        return 0
    user_ids = await broadcast_service.recipients(db, drop.merchant_id)
    markup = _claim_kb(drop_id, drop.btn_label)
    text, photo = drop.text, drop.photo
    sent = 0
    pending: List[BonusDropMessage] = []
    for uid in user_ids:
        try:
            if photo:
                m = await bot.send_photo(uid, photo, caption=text or None, reply_markup=markup)
            else:
                m = await bot.send_message(uid, text, reply_markup=markup)
            pending.append(BonusDropMessage(
                drop_id=drop_id, chat_id=m.chat.id, message_id=m.message_id
            ))
            sent += 1
            if len(pending) >= 200:
                async with db.session() as s:
                    s.add_all(pending)
                    await s.commit()
                pending = []
        except Exception:  # noqa: BLE001 — заблокировал бота / недоступен
            pass
        await asyncio.sleep(_SEND_DELAY)
    if pending:
        async with db.session() as s:
            s.add_all(pending)
            await s.commit()
    await activate_drop(db, drop_id, sent)
    log.info("Бонус-дроп #%s разослан: %s получателей", drop_id, sent)
    return sent


async def claim(
    db: Database, merchant_id: int, drop_id: int, user_id: int
) -> Tuple[str, dict]:
    """Забрать бонус из дропа. Возврат (status, info):

    status: ok | already | ended | not_found.
    info — поля дропа (тексты/кнопка/сумма), снятые до записи, для рендера.
    """
    async with db.session() as s:
        drop = await s.get(BonusDrop, drop_id)
        if drop is None or drop.merchant_id != merchant_id:
            return "not_found", {}
        info = {
            "amount": Decimal(drop.amount_rub),
            "success_text": drop.success_text,
            "success_btn_label": drop.success_btn_label,
            "success_btn_kind": drop.success_btn_kind,
            "success_btn_target": drop.success_btn_target,
            "error_text": drop.error_text,
        }
        now = datetime.utcnow()
        # Забирать можно, пока дроп не завершён (expired) и не вышел срок. Статус
        # «sending» (легаси) тоже допускаем — активации не должны зависеть от конца
        # фоновой рассылки.
        if drop.status == "expired" or (drop.expires_at and now > drop.expires_at):
            return "ended", info
        if await s.get(BonusActivation, {"drop_id": drop_id, "user_id": user_id}):
            return "already", info
        # атомарно занять слот под лимитом
        res = await s.execute(
            update(BonusDrop)
            .where(
                BonusDrop.id == drop_id,
                BonusDrop.status != "expired",
                BonusDrop.claimed_count < BonusDrop.max_claims,
            )
            .values(claimed_count=BonusDrop.claimed_count + 1)
        )
        if res.rowcount != 1:
            await s.rollback()
            return "ended", info
        if not await _deduct_earnings(s, merchant_id, info["amount"]):
            await s.rollback()  # у франшизы нет средств — считаем, что бонусы кончились
            return "ended", info
        await balance_service.credit(s, user_id, info["amount"], merchant_id)
        s.add(BonusActivation(drop_id=drop_id, user_id=user_id))
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            return "already", info
        await s.commit()
        return "ok", info


# ── Удаление через 24ч ───────────────────────────────────────────────────────
async def due_drops(session: AsyncSession, now: datetime) -> List[int]:
    """ID дропов, у которых вышли 24ч и которые ещё не помечены expired."""
    rows = await session.scalars(
        select(BonusDrop.id).where(
            BonusDrop.status != "expired",
            BonusDrop.expires_at.is_not(None),
            BonusDrop.expires_at <= now,
        )
    )
    return list(rows)


async def messages_to_delete(session: AsyncSession, drop_id: int) -> List[Tuple[int, int]]:
    rows = await session.execute(
        select(BonusDropMessage.id, BonusDropMessage.chat_id, BonusDropMessage.message_id)
        .where(BonusDropMessage.drop_id == drop_id, BonusDropMessage.deleted.is_(False))
    )
    return [(r[0], r[1], r[2]) for r in rows.all()]


async def mark_message_deleted(db: Database, msg_row_id: int) -> None:
    async with db.session() as s:
        await s.execute(
            update(BonusDropMessage).where(BonusDropMessage.id == msg_row_id).values(deleted=True)
        )
        await s.commit()


async def mark_drop_expired(db: Database, drop_id: int) -> None:
    async with db.session() as s:
        await s.execute(update(BonusDrop).where(BonusDrop.id == drop_id).values(status="expired"))
        await s.commit()


async def delete_drop_messages(bot, db: Database, drop_id: int) -> int:
    """Удалить все сохранённые сообщения дропа (пост + служебные) через бот.

    Возвращает число фактически удалённых. Используется и для авто-удаления через
    24ч, и для ручного удаления из админки."""
    async with db.session() as s:
        msgs = await messages_to_delete(s, drop_id)
    deleted = 0
    for row_id, chat_id, message_id in msgs:
        try:
            await bot.delete_message(chat_id, message_id)
            deleted += 1
        except Exception:  # noqa: BLE001 — уже удалено/недоступно
            pass
        await mark_message_deleted(db, row_id)
        await asyncio.sleep(0.03)
    return deleted


async def purge_drop(bot, db: Database, drop_id: int) -> int:
    """Остановить активации (status=expired) и удалить все сообщения дропа."""
    await mark_drop_expired(db, drop_id)
    return await delete_drop_messages(bot, db, drop_id)

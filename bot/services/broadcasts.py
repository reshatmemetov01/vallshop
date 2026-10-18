"""Рассылки как сущность: черновик, расписание, результат, клики по кнопкам.

Создаём запись ``Broadcast`` и для немедленной, и для отложенной отправки — её id
нужен, чтобы привязать клики к кнопкам поста (callback ``bck:<id>:<idx>:<target>``)
и чтобы отложенная рассылка пережила перезапуск.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Broadcast, BroadcastClick
from . import broadcast as broadcast_service
from .. import keyboards as kb

log = logging.getLogger("vallshop.broadcasts")


def buttons_of(bc: Broadcast) -> List[dict]:
    """Список кнопок рассылки (из JSON) — пустой, если их нет/битые данные."""
    if not bc.buttons_json:
        return []
    try:
        data = json.loads(bc.buttons_json)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def create(
    session: AsyncSession,
    *,
    merchant_id: int,
    created_by: int,
    text: Optional[str],
    photo: Optional[str],
    buttons: List[dict],
    status: str,
    scheduled_at: Optional[datetime] = None,
) -> Broadcast:
    """Создать запись рассылки и вернуть её (с присвоенным id)."""
    bc = Broadcast(
        merchant_id=merchant_id,
        created_by=created_by,
        text=text,
        photo=photo,
        buttons_json=json.dumps(buttons or [], ensure_ascii=False),
        status=status,
        scheduled_at=scheduled_at,
    )
    session.add(bc)
    await session.flush()  # получить id до коммита
    await session.commit()
    return bc


async def get(
    session: AsyncSession, bc_id: int, merchant_id: Optional[int] = None
) -> Optional[Broadcast]:
    bc = await session.get(Broadcast, bc_id)
    if bc is None:
        return None
    if merchant_id is not None and bc.merchant_id != merchant_id:
        return None  # чужая рассылка — не отдаём
    return bc


async def mark(db: Database, bc_id: int, status: str, **fields) -> None:
    """Обновить статус/счётчики рассылки."""
    async with db.session() as s:
        values = {"status": status, **fields}
        await s.execute(update(Broadcast).where(Broadcast.id == bc_id).values(**values))
        await s.commit()


async def list_scheduled(session: AsyncSession, merchant_id: int) -> List[Broadcast]:
    """Ожидающие отправки (запланированные) рассылки магазина, ближайшие сверху."""
    rows = await session.scalars(
        select(Broadcast)
        .where(Broadcast.merchant_id == merchant_id, Broadcast.status == "scheduled")
        .order_by(Broadcast.scheduled_at.asc())
    )
    return list(rows)


async def recent(session: AsyncSession, merchant_id: int, limit: int = 10) -> List[Broadcast]:
    """Последние отправленные/в процессе/неудавшиеся рассылки магазина."""
    rows = await session.scalars(
        select(Broadcast)
        .where(
            Broadcast.merchant_id == merchant_id,
            Broadcast.status.in_(("sending", "done", "failed")),
        )
        .order_by(Broadcast.id.desc())
        .limit(limit)
    )
    return list(rows)


async def cancel(db: Database, bc_id: int, merchant_id: int) -> bool:
    """Отменить запланированную рассылку (только свою и только пока scheduled)."""
    async with db.session() as s:
        bc = await s.get(Broadcast, bc_id)
        if bc is None or bc.merchant_id != merchant_id or bc.status != "scheduled":
            return False
        bc.status = "canceled"
        await s.commit()
        return True


async def claim_due(db: Database, now: datetime) -> List[tuple]:
    """Атомарно забрать «созревшие» рассылки: перевести в sending, вернуть
    ``[(id, merchant_id, created_by)]``. Защита от повторной отправки в тике."""
    async with db.session() as s:
        rows = list(
            await s.scalars(
                select(Broadcast).where(
                    Broadcast.status == "scheduled",
                    Broadcast.scheduled_at.is_not(None),
                    Broadcast.scheduled_at <= now,
                )
            )
        )
        claimed = []
        for bc in rows:
            bc.status = "sending"
            claimed.append((bc.id, bc.merchant_id, bc.created_by))
        await s.commit()
        return claimed


async def deliver(bot, db: Database, bc_id: int) -> tuple[int, int]:
    """Отправить рассылку её получателям, обновить активных/неактивных и запись.

    Возвращает ``(отправлено, ошибок)``. Статус → done. Клавиатура поста строится
    с tracking-колбэками (``bck:<id>:<idx>:<target>``) для подсчёта кликов.
    """
    async with db.session() as s:
        bc = await s.get(Broadcast, bc_id)
    if bc is None:
        return 0, 0
    user_ids = await broadcast_service.recipients(db, bc.merchant_id)
    markup = kb.broadcast_post_kb(buttons_of(bc), broadcast_id=bc.id)
    sent, failed, ok_ids, blocked_ids = await broadcast_service.run(
        bot, user_ids, text=bc.text, photo=bc.photo, reply_markup=markup
    )
    await broadcast_service.apply_delivery(db, bc.merchant_id, ok_ids, blocked_ids)
    await mark(
        db, bc_id, "done",
        recipients=len(user_ids), sent=sent, failed=failed, sent_at=datetime.utcnow(),
    )
    log.info("Рассылка #%s доставлена: sent=%s failed=%s", bc_id, sent, failed)
    return sent, failed


# ── Клики по кнопкам поста ──────────────────────────────────────────────────
async def record_click(db: Database, broadcast_id: int, btn_idx: int, user_id: int) -> None:
    """Учесть клик по кнопке поста (инкремент счётчика для пользователя)."""
    async with db.session() as s:
        row = await s.get(
            BroadcastClick,
            {"broadcast_id": broadcast_id, "btn_idx": btn_idx, "user_id": user_id},
        )
        if row is None:
            s.add(BroadcastClick(
                broadcast_id=broadcast_id, btn_idx=btn_idx, user_id=user_id, count=1
            ))
        else:
            row.count = (row.count or 0) + 1
        await s.commit()


async def button_stats(session: AsyncSession, broadcast_id: int) -> dict:
    """Клики по кнопкам рассылки: {btn_idx: {"uniq": N, "total": M}}."""
    rows = await session.execute(
        select(
            BroadcastClick.btn_idx,
            func.count(BroadcastClick.user_id),
            func.coalesce(func.sum(BroadcastClick.count), 0),
        )
        .where(BroadcastClick.broadcast_id == broadcast_id)
        .group_by(BroadcastClick.btn_idx)
    )
    out: dict = {}
    for btn_idx, uniq, total in rows.all():
        out[int(btn_idx)] = {"uniq": int(uniq or 0), "total": int(total or 0)}
    return out

"""Рассылка сообщений клиентам магазина (в разрезе мерчанта).

Франшиза рассылает только своим клиентам (User.merchant_id == merchant_id) через
СВОЙ бот. Головной — своим. Отправка идёт с небольшой задержкой (лимиты Telegram)
и не падает на заблокировавших бота пользователях.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from sqlalchemy import select

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, User

log = logging.getLogger("vallshop.broadcast")

# Пауза между отправками (~20 сообщений/сек — безопасно для Telegram).
_DELAY = 0.05


async def recipients(db: Database, merchant_id: int = HEAD_MERCHANT_ID) -> List[int]:
    """Telegram-id клиентов магазина (не заблокированных)."""
    async with db.session() as s:
        rows = await s.scalars(
            select(User.id).where(
                User.merchant_id == merchant_id,
                User.is_blocked.is_(False),
            )
        )
        return list(rows)


async def run(
    bot, user_ids: List[int], *, text: Optional[str], photo: Optional[str] = None
) -> tuple[int, int]:
    """Отправить сообщение списку пользователей. Возвращает (отправлено, ошибок)."""
    sent = failed = 0
    for uid in user_ids:
        try:
            if photo:
                await bot.send_photo(uid, photo, caption=text or None)
            else:
                await bot.send_message(uid, text)
            sent += 1
        except Exception:  # noqa: BLE001 — заблокировал бота / недоступен
            failed += 1
        await asyncio.sleep(_DELAY)
    log.info("Рассылка завершена: отправлено=%s, ошибок=%s", sent, failed)
    return sent, failed

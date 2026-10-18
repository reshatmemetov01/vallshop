"""Рассылка сообщений клиентам магазина (в разрезе мерчанта).

Франшиза рассылает только своим клиентам (User.merchant_id == merchant_id) через
СВОЙ бот. Головной — своим. Отправка идёт с небольшой задержкой (лимиты Telegram)
и не падает на заблокировавших бота пользователях.

По итогам рассылки обновляется достижимость клиентов (``User.is_active``):
кто заблокировал бота — становится неактивным, кто снова принял сообщение —
активным. Обратно в активные человек возвращается и сам — как только пишет боту
(см. ``BlockedMiddleware``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from sqlalchemy import select, update

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, User

log = logging.getLogger("vallshop.broadcast")

# Пауза между отправками (~20 сообщений/сек — безопасно для Telegram).
_DELAY = 0.05

# Маркеры ошибок «пользователь недоступен навсегда» → помечаем неактивным.
# Прочие ошибки (флуд/сеть/временные) достижимость не меняют.
_BLOCK_MARKERS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
    "peer_id_invalid",
    "user is deleted",
)


def _is_block_error(exc: Exception) -> bool:
    """Ошибка означает, что клиент недоступен навсегда (заблокировал/удалён)?"""
    try:  # aiogram может быть не установлен в тестовом окружении сервиса
        from aiogram.exceptions import TelegramForbiddenError

        if isinstance(exc, TelegramForbiddenError):
            return True
    except Exception:  # noqa: BLE001
        pass
    return any(m in str(exc).lower() for m in _BLOCK_MARKERS)


async def recipients(db: Database, merchant_id: int = HEAD_MERCHANT_ID) -> List[int]:
    """Telegram-id клиентов магазина (без забаненных админом)."""
    async with db.session() as s:
        rows = await s.scalars(
            select(User.id).where(
                User.merchant_id == merchant_id,
                User.is_blocked.is_(False),
            )
        )
        return list(rows)


async def run(
    bot,
    user_ids: List[int],
    *,
    text: Optional[str],
    photo: Optional[str] = None,
    reply_markup=None,
) -> tuple[int, int, List[int], List[int]]:
    """Отправить сообщение списку пользователей.

    Возвращает ``(отправлено, ошибок, доставленные_id, недоступные_id)``.
    Недоступные — те, кто заблокировал бота / удалил аккаунт (по тексту ошибки).
    """
    sent = failed = 0
    ok_ids: List[int] = []
    blocked_ids: List[int] = []

    async def _send(uid: int) -> None:
        if photo:
            await bot.send_photo(uid, photo, caption=text or None, reply_markup=reply_markup)
        else:
            await bot.send_message(uid, text, reply_markup=reply_markup)

    for uid in user_ids:
        try:
            await _send(uid)
            sent += 1
            ok_ids.append(uid)
        except Exception as exc:  # noqa: BLE001
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:  # флуд-лимит Telegram — подождать и повторить один раз
                await asyncio.sleep(float(retry_after))
                try:
                    await _send(uid)
                    sent += 1
                    ok_ids.append(uid)
                    await asyncio.sleep(_DELAY)
                    continue
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2
            failed += 1
            if _is_block_error(exc):
                blocked_ids.append(uid)
        await asyncio.sleep(_DELAY)

    log.info(
        "Рассылка завершена: отправлено=%s, ошибок=%s, недоступны=%s",
        sent, failed, len(blocked_ids),
    )
    return sent, failed, ok_ids, blocked_ids


async def apply_delivery(
    db: Database,
    merchant_id: int,
    ok_ids: List[int],
    blocked_ids: List[int],
) -> None:
    """Обновить достижимость клиентов по итогам рассылки.

    Недоступные → ``is_active=False``; успешно доставленные, бывшие неактивными,
    → снова ``is_active=True``.
    """
    def _chunks(seq, n=400):
        for i in range(0, len(seq), n):
            yield seq[i : i + n]

    async with db.session() as s:
        for part in _chunks(blocked_ids):
            await s.execute(
                update(User)
                .where(User.merchant_id == merchant_id, User.id.in_(part))
                .values(is_active=False)
            )
        for part in _chunks(ok_ids):
            await s.execute(
                update(User)
                .where(
                    User.merchant_id == merchant_id,
                    User.id.in_(part),
                    User.is_active.is_(False),
                )
                .values(is_active=True)
            )
        await s.commit()

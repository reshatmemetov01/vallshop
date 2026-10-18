"""Фоновый планировщик отложенных рассылок.

Каждые ``interval`` секунд забирает «созревшие» рассылки (scheduled_at <= now) и
отправляет их через бот нужного магазина. Переживает перезапуск: расписание в БД.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..db import Database
from . import broadcasts as broadcasts_service
from . import bonuses as bonuses_service

log = logging.getLogger("vallshop.bcscheduler")


async def run_broadcast_scheduler(
    db: Database, bots: dict, interval: float = 30.0
) -> None:
    """Бесконечный цикл отправки отложенных рассылок."""
    while True:
        try:
            await _tick(db, bots)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — планировщик не должен падать
            log.exception("Ошибка в цикле планировщика рассылок")
        await asyncio.sleep(interval)


async def _tick(db: Database, bots: dict) -> None:
    due = await broadcasts_service.claim_due(db, datetime.utcnow())
    for bc_id, merchant_id, created_by in due:
        bot = (bots or {}).get(merchant_id)
        if bot is None:
            # Бот магазина не поднят (напр. франшиза без рестарта) — пометим и дальше.
            await broadcasts_service.mark(db, bc_id, "failed", reason="бот магазина недоступен")
            log.warning("Рассылка #%s: нет бота для merchant_id=%s", bc_id, merchant_id)
            continue
        try:
            sent, failed = await broadcasts_service.deliver(bot, db, bc_id)
        except Exception:  # noqa: BLE001
            log.exception("Рассылка #%s: ошибка отправки", bc_id)
            await broadcasts_service.mark(db, bc_id, "failed", reason="ошибка отправки")
            continue
        try:
            await bot.send_message(
                created_by,
                f"🕒 Запланированная рассылка отправлена.\n"
                f"Отправлено: <b>{sent}</b> · не доставлено: <b>{failed}</b>.",
            )
        except Exception:  # noqa: BLE001
            pass


async def run_bonus_cleaner(db: Database, bots: dict, interval: float = 60.0) -> None:
    """Удаляет пост и служебные сообщения бонус-дропов спустя 24ч (TTL)."""
    while True:
        try:
            await _tick_bonus(db, bots)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле очистки бонус-дропов")
        await asyncio.sleep(interval)


async def _tick_bonus(db: Database, bots: dict) -> None:
    async with db.session() as s:
        due = await bonuses_service.due_drops(s, datetime.utcnow())
    for drop_id in due:
        async with db.session() as s:
            drop = await bonuses_service.get_drop(s, drop_id)
            msgs = await bonuses_service.messages_to_delete(s, drop_id)
        bot = (bots or {}).get(drop.merchant_id) if drop else None
        if bot is None:
            await bonuses_service.mark_drop_expired(db, drop_id)
            continue
        for row_id, chat_id, message_id in msgs:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:  # noqa: BLE001 — сообщение уже удалено/недоступно
                pass
            await bonuses_service.mark_message_deleted(db, row_id)
            await asyncio.sleep(0.03)
        await bonuses_service.mark_drop_expired(db, drop_id)
        log.info("Бонус-дроп #%s: сообщения удалены (24ч)", drop_id)

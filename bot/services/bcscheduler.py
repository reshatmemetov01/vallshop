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

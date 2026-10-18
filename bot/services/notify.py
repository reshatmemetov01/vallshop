"""Уведомления администраторам магазина."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterable

from aiogram import Bot

log = logging.getLogger("vallshop.notify")


async def notify_admins(bot: Bot, admin_ids: Iterable[int], text: str) -> None:
    """Отправить сообщение всем администраторам (ошибки игнорируем)."""
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 — один недоступный админ не должен ломать рассылку
            log.warning("Не удалось уведомить админа %s", admin_id)


async def _send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось отправить уведомление %s", chat_id)


async def notify_topup(db, config, bots, *, user_id: int, amount, merchant_id: int) -> None:
    """Уведомить админов о пополнении баланса пользователем.

    • головным админам (``config.admin_ids``) через головной бот — ВСЕГДА (в т.ч.
      о пополнениях через франшизу, с названием франшизы);
    • владельцу франшизы (``Merchant.admin_user_id``) через её бот — если франшиза.

    Того, кто сам пополнил (если это админ/владелец), не уведомляем.
    """
    from ..db.models import HEAD_MERCHANT_ID, Merchant, User
    from . import balance as balance_service
    from .. import texts

    bots = bots or {}
    async with db.session() as s:
        user = await s.get(User, {"id": user_id, "merchant_id": merchant_id})
        username = user.username if user else None
        balance = await balance_service.get_balance(s, user_id, merchant_id)
        title = None
        franchise_admin = None
        if merchant_id != HEAD_MERCHANT_ID:
            m = await s.get(Merchant, merchant_id)
            title = (m.title if m else None) or f"#{merchant_id}"
            franchise_admin = m.admin_user_id if m else None

    who = f"@{username}" if username else f"ID <code>{user_id}</code>"
    amt, bal = texts.rub(amount), texts.rub(balance)
    body = (f"Пользователь: {who}\nСумма: <b>{amt}</b>\nБаланс пользователя: <b>{bal}</b>")

    # Головным админам.
    head_bot = bots.get(HEAD_MERCHANT_ID)
    if head_bot is not None and config is not None:
        if merchant_id == HEAD_MERCHANT_ID:
            head_text = "💰 <b>Пополнение баланса</b>\n" + body
        else:
            head_text = f"💰 <b>Пополнение через франшизу «{title}»</b>\n" + body
        for admin_id in getattr(config, "admin_ids", []) or []:
            if admin_id == user_id:
                continue  # себя не уведомляем
            await _send(head_bot, admin_id, head_text)

    # Владельцу франшизы.
    if merchant_id != HEAD_MERCHANT_ID and franchise_admin and franchise_admin != user_id:
        fbot = bots.get(merchant_id)
        if fbot is not None:
            await _send(fbot, franchise_admin, "💰 <b>Пополнение баланса</b>\n" + body)

"""Фоновый поллер: дотягивает топап-заказы в статусе FULFILLING до финала.

Нужен, когда поставщик LioGames выдаёт код не мгновенно (status=PROCESSING).
Опрашивает order-status с учётом интервала и уведомляет покупателя.
"""

from __future__ import annotations

import asyncio
import logging

from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select

from liogames import LioGamesClient
from fazercard import FazerCardClient

from ..config import BotConfig
from ..db import Database
from ..db.models import Order, OrderStatus, TopUp, TopUpStatus, Variant
from ..services import balance as balance_service
from ..services import catalog as catalog_service
from ..services import notify as notify_service
from ..services import orders as order_service
from ..services import referral as referral_service
from ..services import settings as settings_service
from .. import keyboards as kb
from .. import texts

log = logging.getLogger("vallshop.poller")

# Через сколько минут «зависший» в FULFILLING заказ FazerCard (поставщик не
# отдаёт финальный статус) считается подозрительным — один раз уведомляем
# админа, чтобы деньги покупателя не застряли молча навсегда.
FULFILL_STUCK_MINUTES = 30
_STUCK_SENTINEL = "⏳ завис в обработке у поставщика (нет финального статуса)"


async def run_fulfillment_poller(
    bot: Bot,
    db: Database,
    liog: LioGamesClient,
    fzr: FazerCardClient | None = None,
    config: BotConfig | None = None,
    interval: float = 20.0,
) -> None:
    """Бесконечный цикл опроса незавершённых заказов (LioGames + FazerCard)."""
    while True:
        try:
            await _tick(bot, db, liog, fzr, config)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — поллер не должен падать
            log.exception("Ошибка в цикле поллера выдачи")
        await asyncio.sleep(interval)


async def _tick(
    bot: Bot,
    db: Database,
    liog: LioGamesClient,
    fzr: FazerCardClient | None,
    config: BotConfig | None = None,
) -> None:
    async with db.session() as session:
        orders = list(
            await session.scalars(
                select(Order).where(Order.status == OrderStatus.FULFILLING)
            )
        )

    for order in orders:
        async with db.session() as session:
            fresh = await session.get(Order, order.id)
            if not fresh or fresh.status != OrderStatus.FULFILLING:
                continue

            refunded = False
            needs_action = False
            stuck = False
            refund_text = None
            if fresh.supplier == "fazercard":
                if fzr is None:
                    continue  # клиент не настроен — пропускаем
                status = await order_service.poll_fazercard(session, fresh, fzr)
                if status == OrderStatus.FULFILLING:
                    # Поставщик не отдал финальный статус. Если заказ висит
                    # слишком долго — один раз зовём админа (не трогаем деньги
                    # автоматически: заказ может завершиться позже).
                    age = datetime.utcnow() - (fresh.created_at or datetime.utcnow())
                    if age >= timedelta(minutes=FULFILL_STUCK_MINUTES) and not fresh.fail_reason:
                        fresh.fail_reason = _STUCK_SENTINEL
                        stuck = True
                if status == OrderStatus.FAILED:
                    auto = await settings_service.get_bool(
                        session, settings_service.AUTO_REFUND, True
                    )
                    if auto:
                        # buy-on-demand оплачивался с баланса — возвращаем деньги.
                        refund = Decimal(fresh.price_usd) * (fresh.quantity or 1)
                        await balance_service.credit(session, fresh.user_id, refund)
                        fresh.status = OrderStatus.REFUNDED
                        status = OrderStatus.REFUNDED
                        refunded = True
                        await referral_service.reverse_for_order(session, fresh)
                        refund_text = await settings_service.get(
                            session, settings_service.REFUND_TEXT, texts.FULFILL_REFUNDED
                        )
                    else:
                        # Автовозврат выключен — в ручную обработку, деньги остаются.
                        fresh.status = OrderStatus.NEEDS_ACTION
                        fresh.fail_reason = fresh.fail_reason or "поставщик отклонил заказ"
                        status = OrderStatus.NEEDS_ACTION
                        needs_action = True
            else:
                status = await order_service.poll_topup(session, fresh, liog)

            user_id = fresh.user_id
            code = fresh.delivery_code
            ref = fresh.client_ref
            order_id = fresh.id
            variant_id = fresh.variant_id
            reason = fresh.fail_reason or "—"
            refund_amount = Decimal(fresh.price_usd) * (fresh.quantity or 1)
            delivered_text = texts.TOPUP_ACCOUNT_DELIVERED
            item_name = "—"
            variant = await session.get(Variant, fresh.variant_id)
            if variant is not None:
                item_name = variant.title
                if status == OrderStatus.COMPLETED and not code:
                    delivered_text = await catalog_service.resolve_text(
                        session, variant, "delivered_text", texts.TOPUP_ACCOUNT_DELIVERED
                    )
            await session.commit()

        admin_ids = list(config.admin_ids) if config else []
        amount_str = texts.rub(refund_amount)
        done_kb = kb.order_done_kb(order_id, variant_id)
        if status == OrderStatus.COMPLETED and code:
            await _notify(
                bot, user_id, texts.DELIVERY_SUCCESS.format(code=code), done_kb
            )
        elif status == OrderStatus.COMPLETED:
            # Доставка на аккаунт/username без кода (топап/Telegram) — настраиваемый текст.
            if delivered_text:
                await _notify(bot, user_id, delivered_text, done_kb)
        elif status == OrderStatus.REFUNDED and refunded:
            await _notify(
                bot, user_id,
                settings_service.fmt(
                    refund_text or texts.FULFILL_REFUNDED, ref=ref, amount=amount_str
                ),
            )
            await notify_service.notify_admins(
                bot, admin_ids,
                f"↩️ Заказ #{order_id} (<b>{item_name}</b>) провалился и "
                f"возвращён покупателю (<b>{amount_str}</b>).\nПричина: {reason}",
            )
        elif status == OrderStatus.NEEDS_ACTION and needs_action:
            await notify_service.notify_admins(
                bot, admin_ids,
                f"❗️ Заказ #{order_id} (<b>{item_name}</b>) требует ручной выдачи.\n"
                f"Причина: {reason}\n"
                "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
            )
        elif status == OrderStatus.FAILED:
            await _notify(bot, user_id, texts.FULFILL_FAILED.format(ref=ref))
            await notify_service.notify_admins(
                bot, admin_ids,
                f"⚠️ Заказ #{order_id} (<b>{item_name}</b>): ошибка выдачи.\n"
                f"Причина: {reason}",
            )
        elif stuck:
            await notify_service.notify_admins(
                bot, admin_ids,
                f"⏳ Заказ #{order_id} (<b>{item_name}</b>) висит в обработке у "
                f"поставщика более {FULFILL_STUCK_MINUTES} мин — финального статуса нет.\n"
                f"Сумма: <b>{amount_str}</b>. Проверьте вручную: «Админ-панель → "
                f"Текущие заказы». Если поставщик вернул деньги — заказ можно "
                f"отменить (возврат на баланс покупателя).",
            )


async def run_smartprice_poller(
    bot: Bot,
    db: Database,
    fzr: FazerCardClient | None = None,
    config: BotConfig | None = None,
    interval: float = 600.0,
) -> None:
    """Фоновый пересчёт умных цен по живому закупу (порог 1%) + алерты о росте."""
    from ..services import smartprice as smartprice_service
    while True:
        try:
            if fzr is not None:
                admin_ids = config.admin_ids if config else None
                await smartprice_service.recompute_all(
                    db, fzr, bot=bot, admin_ids=admin_ids
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в поллере умных цен")
        await asyncio.sleep(interval)


async def run_topup_poller(
    bot: Bot, db: Database, provider, payhot=None, paydex=None, interval: float = 30.0
) -> None:
    """Фоновая авто-проверка пополнений (подстраховка вебхука).

    Опрашивает провайдера по PENDING-пополнениям: при оплате — зачисляет
    баланс и уведомляет, при истечении срока — помечает EXPIRED. Операции
    идемпотентны (повтор с вебхуком безопасен)."""
    while True:
        try:
            await _topup_tick(bot, db, provider, payhot, paydex)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле поллера пополнений")
        await asyncio.sleep(interval)


def _provider_and_ref(topup, provider, payhot, paydex=None):
    """Выбрать провайдера и ключ для проверки статуса по topup.provider."""
    if topup.provider == "payhot" and payhot is not None:
        return payhot, topup.provider_order_id
    if topup.provider == "paydex" and paydex is not None:
        return paydex, topup.provider_order_id
    return provider, topup.client_ref


async def _topup_tick(bot: Bot, db: Database, provider, payhot=None, paydex=None) -> None:
    async with db.session() as session:
        pending = list(
            await session.scalars(
                select(TopUp).where(TopUp.status == TopUpStatus.PENDING)
            )
        )

    for t in pending:
        async with db.session() as session:
            fresh = await session.get(TopUp, t.id)
            if not fresh or fresh.status != TopUpStatus.PENDING:
                continue
            if fresh.expires_at and datetime.utcnow() > fresh.expires_at:
                fresh.status = TopUpStatus.EXPIRED
                await session.commit()
                continue
            prov, ref = _provider_and_ref(fresh, provider, payhot, paydex)
            if prov is None or not ref:
                continue
            try:
                update = await prov.get_status(ref)
            except Exception:  # noqa: BLE001 — провайдер недоступен, попробуем позже
                continue
            if getattr(update, "status", None) != "paid":
                continue
            fresh.status = TopUpStatus.PAID
            fresh.tx_hash = getattr(update, "tx_hash", None)
            fresh.paid_at = datetime.utcnow()
            await balance_service.credit(session, fresh.user_id, fresh.amount_usd)
            await session.commit()
            user_id, credit = fresh.user_id, fresh.amount_usd
            balance = await balance_service.get_balance(session, user_id)

        await _notify(
            bot, user_id,
            texts.TOPUP_SUCCESS.format(
                credit=texts.rub(credit), balance=texts.rub(balance)
            ),
        )


async def _notify(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось отправить сообщение user=%s", user_id)

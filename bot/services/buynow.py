"""«Купить сразу»: доплата-пополнение под конкретный товар.

Когда у клиента не хватает баланса на товар, бот формирует пополнение на
НЕДОСТАЮЩУЮ сумму с «намерением покупки» (``TopUp.buy_json``). Баланс при этом
не трогается. После того как оплата подтвердилась и баланс зачислен (любым
путём — вебхук/поллер/ручная проверка), вызывается :func:`on_topup_paid_buy`,
который докупает товар с баланса (``purchase_from_balance``) и выдаёт его.

Идемпотентность: покупка выполняется только один раз — помечается
``TopUp.buy_order_id`` (id заказа, либо ``FALLBACK`` = обработано, но товар не
выдан, деньги остались на балансе).
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Optional

from aiogram import Bot
from sqlalchemy import select

from ..db import Database
from ..db.models import Order, OrderStatus, TopUp, TopUpStatus
from .. import texts
from . import balance as balance_service
from . import catalog as catalog_service
from . import notify as notify_service
from . import orders as order_service
from .orders import InsufficientBalance, SupplierError

log = logging.getLogger("vallshop.buynow")

# buy_order_id == FALLBACK → обработано, но товар авто-выдать не удалось
# (средства остались на балансе клиента). None → ещё не обработано.
FALLBACK = -1


def make_intent(
    *, variant_id: int, qty: int, unit_price, owner_price=None, title: str = "",
    fields: Optional[dict] = None, smm_link: Optional[str] = None,
    smm_quantity: Optional[int] = None,
) -> str:
    """Сериализовать намерение покупки для TopUp.buy_json."""
    return json.dumps(
        {
            "vid": int(variant_id),
            "qty": int(qty),
            "unit_price": str(unit_price),
            "owner_price": str(owner_price) if owner_price is not None else None,
            "title": title or "",
            "fields": fields or None,
            "smm_link": smm_link,
            "smm_quantity": smm_quantity,
        },
        ensure_ascii=False,
    )


class BuyOutcome:
    """Результат авто-покупки после оплаты (что сообщить клиенту)."""

    def __init__(self, kind: str, *, order_id=None, codes=None, variant_id=None,
                 title="товар", ref=None):
        self.kind = kind          # "completed" | "fulfilling" | "failed" | "fallback"
        self.order_id = order_id
        self.codes = codes or []
        self.variant_id = variant_id
        self.title = title
        self.ref = ref


async def _settle(session, topup: TopUp, suppliers: dict, config) -> Optional[BuyOutcome]:
    """Докупить товар за уже зачисленное пополнение. Пишет buy_order_id."""
    try:
        intent = json.loads(topup.buy_json)
    except Exception:  # noqa: BLE001
        topup.buy_order_id = FALLBACK
        await session.commit()
        return None

    variant = await catalog_service.get_variant(session, int(intent["vid"]))
    if variant is None or not variant.is_active:
        topup.buy_order_id = FALLBACK
        await session.commit()
        return BuyOutcome("fallback")

    unit_price = Decimal(str(intent["unit_price"]))
    owner_price = Decimal(str(intent["owner_price"])) if intent.get("owner_price") else None
    qty = int(intent.get("qty") or 1)
    fields = intent.get("fields")
    topup_id = topup.id
    v_id = variant.id
    v_title = variant.title

    try:
        order, codes = await order_service.purchase_from_balance(
            session, user_id=topup.user_id, variant=variant,
            unit_price=unit_price, quantity=qty, merchant_id=topup.merchant_id,
            owner_unit_price=owner_price,
            fzr=suppliers.get("fzr"), fragment=suppliers.get("fragment"),
            soc=suppliers.get("soc"), panels=suppliers.get("panels"),
            vpn=suppliers.get("vpn"),
            public_base_url=getattr(config, "public_base_url", None),
            topup_fields=fields,
            smm_link=intent.get("smm_link"), smm_quantity=intent.get("smm_quantity"),
            source=topup.source or "bot",
        )
    except (InsufficientBalance, SupplierError):
        await session.rollback()
        fresh = await session.get(TopUp, topup_id)
        if fresh and fresh.buy_order_id is None:
            fresh.buy_order_id = FALLBACK
            await session.commit()
        return BuyOutcome("fallback")
    except Exception:  # noqa: BLE001
        log.exception("buynow: ошибка авто-покупки topup=%s", topup_id)
        await session.rollback()
        fresh = await session.get(TopUp, topup_id)
        if fresh and fresh.buy_order_id is None:
            fresh.buy_order_id = FALLBACK
            await session.commit()
        return BuyOutcome("fallback")

    # Сохранить собранные данные в заказ (для раздела «Не выполненные»).
    if fields:
        collected = [{"label": k, "value": v} for k, v in fields.items()]
        order.fields_json = json.dumps(collected, ensure_ascii=False)
    status = order.status
    order_id = order.id
    ref = order.client_ref
    topup.buy_order_id = order_id
    await session.commit()

    if status == OrderStatus.COMPLETED:
        return BuyOutcome("completed", order_id=order_id, codes=codes, variant_id=v_id, title=v_title, ref=ref)
    if status == OrderStatus.FULFILLING:
        return BuyOutcome("fulfilling", order_id=order_id, variant_id=v_id, title=v_title, ref=ref)
    return BuyOutcome("failed", order_id=order_id, variant_id=v_id, title=v_title, ref=ref)


def _pick_bot(bots, merchant_id, default):
    if bots:
        return bots.get(merchant_id, default)
    return default


async def on_topup_paid_buy(
    db: Database, bot: Bot, *, topup_id: int, bots: dict | None, config, suppliers: dict,
) -> None:
    """Обработать оплаченное «доплата-под-товар» пополнение: купить и выдать.

    Вызывается ПОСЛЕ зачисления баланса. Идемпотентно (buy_order_id).
    """
    async with db.session() as session:
        topup = await session.get(TopUp, topup_id)
        if (
            topup is None
            or not topup.buy_json
            or topup.buy_order_id is not None
            or topup.status != TopUpStatus.PAID
        ):
            return
        user_id = topup.user_id
        mid = topup.merchant_id
        credit = topup.amount_usd
        outcome = await _settle(session, topup, suppliers, config)

    cbot = _pick_bot(bots, mid, bot)

    # Данные для сообщений (цены/описания) — отдельной сессией.
    if outcome and outcome.kind in ("completed", "fulfilling", "failed"):
        async with db.session() as session:
            variant = await catalog_service.get_variant(session, outcome.variant_id)
            balance = await balance_service.get_balance(session, user_id, merchant_id=mid)
            delivered_text = await catalog_service.resolve_text(
                session, variant, "delivered_text", ""
            ) if variant else ""
            pending_note = await catalog_service.resolve_text(
                session, variant, "pending_text", texts.PENDING_NOTE_DEFAULT
            ) if variant else texts.PENDING_NOTE_DEFAULT
            order = await session.get(Order, outcome.order_id)
            code = order.delivery_code if order else None
            needs_action = bool(order and order.status == OrderStatus.NEEDS_ACTION)
        item = outcome.title or "товар"

        if outcome.kind == "completed":
            if outcome.codes:
                codes_text = "\n".join(f"<code>{c}</code>" for c in outcome.codes)
                text = texts.PURCHASE_SUCCESS.format(
                    item=item, qty=1, total=texts.rub(credit),
                    balance=texts.rub(balance), codes=codes_text,
                )
            elif code:
                text = texts.DELIVERY_SUCCESS.format(code=code)
            elif delivered_text:
                text = delivered_text
            else:
                text = "✅ Заказ выполнен!"
        elif outcome.kind == "fulfilling":
            note = f"\n\n{pending_note}" if pending_note else ""
            text = texts.PURCHASE_PENDING.format(
                item=item, qty=1, total=texts.rub(credit),
                balance=texts.rub(balance), note=note,
            )
        else:  # failed
            text = texts.FULFILL_FAILED.format(ref=outcome.ref or outcome.order_id)

        await _notify(cbot, user_id, text)
        if needs_action:
            await notify_service.notify_admins(
                cbot, config.admin_ids,
                f"❗️ Заказ #{outcome.order_id} (<b>{item}</b>) требует ручной выдачи.\n"
                "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
            )
        return

    # fallback: деньги зачислены, товар авто-выдать не удалось.
    async with db.session() as session:
        balance = await balance_service.get_balance(session, user_id, merchant_id=mid)
    await _notify(
        cbot, user_id,
        texts.BUY_FALLBACK_CREDITED.format(
            credit=texts.rub(credit), balance=texts.rub(balance)
        ),
    )
    await notify_service.notify_topup(
        db, config, bots, user_id=user_id, amount=credit, merchant_id=mid
    )


async def settle_pending_buys(db: Database, bot: Bot, *, bots, config, suppliers) -> None:
    """Подстраховка: добить оплаченные buy-интенты, по которым покупка не прошла
    (например, вебхук зачислил баланс, но авто-покупка упала)."""
    async with db.session() as session:
        rows = list(
            await session.scalars(
                select(TopUp.id).where(
                    TopUp.status == TopUpStatus.PAID,
                    TopUp.buy_json.is_not(None),
                    TopUp.buy_order_id.is_(None),
                )
            )
        )
    for tid in rows:
        await on_topup_paid_buy(
            db, bot, topup_id=tid, bots=bots, config=config, suppliers=suppliers
        )


async def _notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001
        log.warning("buynow: не удалось уведомить user=%s", user_id)

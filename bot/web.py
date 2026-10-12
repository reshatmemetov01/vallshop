"""HTTP-сервер для вебхуков платёжного провайдера (BoltUtil).

Запускается, только если задан PUBLIC_BASE_URL. Проверяет HMAC-подпись,
находит платёж, помечает оплаченным, запускает выдачу и уведомляет покупателя.
Эндпоинт идемпотентен: повторный вебхук по уже обработанному заказу — no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiohttp import web
from sqlalchemy import select

from liogames import LioGamesClient

from .db import Database
from .db.models import Order, OrderStatus, Payment, PaymentStatus, TopUp, TopUpStatus
from .payments import PaymentProvider
from .services import balance as balance_service
from .services import catalog as catalog_service
from .services import orders as order_service
from . import texts

log = logging.getLogger("vallshop.web")


def build_app(
    *, bot: Bot, db: Database, provider: PaymentProvider, liog: LioGamesClient,
    config=None, payhot: PaymentProvider = None, paydex: PaymentProvider = None,
) -> web.Application:
    app = web.Application()

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def offer_page(request: web.Request) -> web.Response:
        from . import legal
        return web.Response(text=legal.offer_html(config), content_type="text/html")

    async def landing_page(request: web.Request) -> web.Response:
        from . import legal
        return web.Response(text=legal.landing_html(config), content_type="text/html")

    async def agreement_page(request: web.Request) -> web.Response:
        from . import legal
        return web.Response(text=legal.agreement_html(config), content_type="text/html")

    async def privacy_page(request: web.Request) -> web.Response:
        from . import legal
        return web.Response(text=legal.privacy_html(config), content_type="text/html")

    async def vpn_sub(request: web.Request) -> web.Response:
        """Подписка VPN: отдаёт base64-список vless:// для клиентов (Happ и т.п.)."""
        token = request.match_info.get("token", "")
        from .db.models import VpnSubscription
        async with db.session() as session:
            sub = await session.scalar(
                select(VpnSubscription).where(VpnSubscription.token == token)
            )
            body = sub.config_b64 if sub and sub.config_b64 else ""
        # Стандартный формат подписки: тело = base64 из строк конфигов.
        return web.Response(text=body, content_type="text/plain")

    async def bolt_webhook(request: web.Request) -> web.Response:
        raw = await request.read()
        if not provider.verify_webhook(raw, dict(request.headers)):
            log.warning("Вебхук с неверной подписью отклонён")
            return web.json_response({"ok": False}, status=403)

        update = provider.parse_webhook(raw)
        # Обработка идемпотентна. BoltUtil требует РОВНО {"status":"SUCCESS"},
        # иначе продолжит повторные попытки доставки.
        await _apply_payment(bot, db, liog, update)
        return web.json_response({"status": "SUCCESS"})

    async def payhot_webhook(request: web.Request) -> web.Response:
        if payhot is None:
            return web.json_response({"ok": False}, status=404)
        raw = await request.read()
        if not payhot.verify_webhook(raw, dict(request.headers)):
            log.warning("PayHot: вебхук с неверной подписью отклонён")
            return web.json_response({"ok": False}, status=403)
        update = payhot.parse_webhook(raw)
        await _apply_payment(bot, db, liog, update)
        return web.json_response({"ok": True})

    async def paydex_webhook(request: web.Request) -> web.Response:
        if paydex is None:
            return web.json_response({"ok": False}, status=404)
        raw = await request.read()
        if not paydex.verify_webhook(raw, dict(request.headers)):
            log.warning("Paydex: вебхук с неверной подписью отклонён")
            # Paydex: при неверной подписи ожидает 401.
            return web.json_response({"ok": False}, status=401)
        update = paydex.parse_webhook(raw)
        await _apply_payment(bot, db, liog, update)
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", landing_page)
    app.router.add_get("/offer", offer_page)
    app.router.add_get("/terms", offer_page)   # алиас
    app.router.add_get("/agreement", agreement_page)
    app.router.add_get("/privacy", privacy_page)
    app.router.add_get("/sub/{token}", vpn_sub)
    # Принимаем вебхук на нескольких путях — на случай, если в кабинете
    # BoltUtil указан другой (например /bolt/callback).
    for path in ("/bolt/webhook", "/bolt/callback", "/callback"):
        app.router.add_post(path, bolt_webhook)
    app.router.add_post("/payhot/webhook", payhot_webhook)
    app.router.add_post("/paydex/webhook", paydex_webhook)
    return app


async def _apply_payment(bot, db: Database, liog, update) -> None:
    if update.status != "paid":
        return

    # Пополнение баланса? (client_ref вида tu-...)
    ref = update.client_ref or ""
    if ref.startswith("tu-"):
        await _apply_topup(bot, db, update)
        return

    await _apply_order_payment(bot, db, liog, update)


async def _apply_topup(bot, db: Database, update) -> None:
    async with db.session() as session:
        topup = await balance_service.get_topup_by_ref(session, update.client_ref)
        if topup is None or topup.status == TopUpStatus.PAID:
            return  # не найдено или уже зачислено (идемпотентность)
        topup.status = TopUpStatus.PAID
        topup.tx_hash = update.tx_hash
        topup.paid_at = datetime.utcnow()
        merchant_id = topup.merchant_id
        await balance_service.credit(
            session, topup.user_id, topup.amount_usd, merchant_id=merchant_id
        )
        await session.flush()
        user_id = topup.user_id
        credit = topup.amount_usd
        await session.commit()
        balance = await balance_service.get_balance(session, user_id, merchant_id=merchant_id)

    await _notify(
        bot,
        user_id,
        texts.TOPUP_SUCCESS.format(
            credit=texts.rub(credit), balance=texts.rub(balance)
        ),
    )


async def _apply_order_payment(bot, db: Database, liog, update) -> None:
    async with db.session() as session:
        payment = None
        if update.provider_order_id:
            payment = await session.scalar(
                select(Payment).where(
                    Payment.provider_order_id == update.provider_order_id
                )
            )
        if payment is None and update.client_ref:
            order = await session.scalar(
                select(Order).where(Order.client_ref == update.client_ref)
            )
            if order is not None:
                payment = await session.scalar(
                    select(Payment).where(Payment.order_id == order.id)
                )
        if payment is None:
            log.warning("Вебхук: платёж не найден (%s)", update.provider_order_id)
            return

        order = await session.get(Order, payment.order_id)
        if order is None or order.status in (
            OrderStatus.COMPLETED,
            OrderStatus.FULFILLING,
        ):
            return  # уже обработано

        payment.status = PaymentStatus.PAID
        payment.tx_hash = update.tx_hash
        payment.paid_at = datetime.utcnow()
        order.status = OrderStatus.PAID
        await session.flush()

        variant = await catalog_service.get_variant(session, order.variant_id)
        status = await order_service.fulfill(session, order, variant, liog)
        user_id = order.user_id
        code = order.delivery_code
        ref = order.client_ref
        await session.commit()

    if status == OrderStatus.COMPLETED and code:
        await _notify(bot, user_id, texts.DELIVERY_SUCCESS.format(code=code))
    elif status == OrderStatus.FULFILLING:
        await _notify(bot, user_id, texts.FULFILLING)
    else:
        await _notify(bot, user_id, texts.FULFILL_FAILED.format(ref=ref))


async def _notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось уведомить user=%s", user_id)

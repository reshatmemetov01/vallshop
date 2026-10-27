"""Пополнение баланса на сайте.

Переиспользуем платёжный слой бота (PayHot — карта, Paydex — СБП) и общую БД.
Важно: ЗАЧИСЛЕНИЕ делает бот — его фоновый поллер пополнений и вебхуки
обрабатывают любой PENDING-топап в общей БД, кем бы он ни был создан. Сайту
достаточно СОЗДАТЬ запись топапа и счёт у провайдера и вернуть ссылку оплаты.

На старте поддерживаем способы с редиректом на страницу оплаты (checkout_url):
card (PayHot) и sbp/sbp_ph (Paydex/PayHot). USDT-крипта (адрес+сеть) — позже.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BotConfig
from bot.db.models import TopUp, WebAccount
from bot.payments import build_paydex, build_payhot, build_provider
from bot.payments.boltutil import MIN_PAYMENT_USDT
from bot.services import balance as balance_service
from bot.services import paymethods as paymethods_service
from bot.services import rates as rates_service
from bot.services import settings as settings_service

from ..config import settings
from . import wallet as wallet_service

# Способы с редиректом на хостинговую страницу оплаты (подходят для сайта):
# card (PayHot), sbp/sbp_ph (Paydex/PayHot), usdt (BoltUtil — своя hosted-страница).
_WEB_METHODS = {"card", "sbp", "sbp_ph", "usdt"}

TOPUP_MIN = balance_service.TOPUP_MIN
TOPUP_MAX = balance_service.TOPUP_MAX

_config: Optional[BotConfig] = None
_providers: Optional[dict] = None


class TopupError(Exception):
    """Ошибка пополнения (текст пригоден для показа клиенту)."""


def _cfg() -> BotConfig:
    global _config
    if _config is None:
        _config = BotConfig.from_env()
    return _config


def _bolt_configured(cfg: BotConfig) -> bool:
    """BoltUtil реально настроен (не mock) — только тогда показываем USDT на сайте."""
    return bool(cfg.bolt_api_key and cfg.bolt_secret) and not cfg.mock_payments


def _provs() -> dict:
    """Карта provider_name -> провайдер (или None, если не настроен)."""
    global _providers
    if _providers is None:
        cfg = _cfg()
        _providers = {
            "payhot": build_payhot(cfg),
            "paydex": build_paydex(cfg),
            # BoltUtil — только если реально настроен (иначе mock с фейковой ссылкой).
            "bolt": build_provider(cfg) if _bolt_configured(cfg) else None,
        }
    return _providers


def _q(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def available_methods(session: AsyncSession) -> List[dict]:
    """Способы оплаты для сайта: включены в админке, провайдер настроен, редирект."""
    provs = _provs()
    avail = {
        "payhot": provs.get("payhot") is not None,
        "paydex": provs.get("paydex") is not None,
        "bolt": provs.get("bolt") is not None,
    }
    methods = await paymethods_service.client_methods(session, available=avail)
    return [
        {"id": m["id"], "label": m["label"]}
        for m in methods
        if m["id"] in _WEB_METHODS
    ]


async def create_topup(
    session: AsyncSession, account: WebAccount, amount: Decimal, method: str
) -> TopUp:
    """Создать пополнение и счёт у провайдера. Вернуть TopUp с checkout_url."""
    amount = _q(amount)
    if amount < TOPUP_MIN or amount > TOPUP_MAX:
        raise TopupError(
            f"Сумма пополнения — от {int(TOPUP_MIN)} до {int(TOPUP_MAX)} ₽"
        )
    if method not in _WEB_METHODS:
        raise TopupError("Этот способ оплаты недоступен на сайте")
    if not await paymethods_service.is_enabled(session, method):
        raise TopupError("Способ оплаты временно отключён")

    provider_name = paymethods_service.PROVIDER_OF.get(method)
    prov = _provs().get(provider_name)
    if prov is None:
        raise TopupError("Способ оплаты недоступен")

    cfg = _cfg()
    # USDT: клиент вводит сумму в ₽ (её и зачислим), а платит криптой по курсу.
    if method == "usdt":
        rate = await _usdt_rate(session)
        pay_amount = _q(amount / rate)
        if pay_amount < MIN_PAYMENT_USDT:
            min_rub = int((MIN_PAYMENT_USDT * rate).to_integral_value(rounding=ROUND_HALF_UP))
            raise TopupError(f"Для оплаты криптой минимум {min_rub} ₽")
        net = cfg.bolt_network
        notify_url = cfg.notify_url
    else:
        pay_amount = amount
        net = paymethods_service.method_code(method)
        notify_url = (
            cfg.paydex_notify_url if provider_name == "paydex" else cfg.payhot_notify_url
        )

    wallet_id = await wallet_service.ensure_wallet(session, account)
    # amount_usd хранит сумму ЗАЧИСЛЕНИЯ — у нас это всегда рубли.
    topup = await balance_service.create_topup(
        session, user_id=wallet_id, amount_usd=amount
    )
    try:
        invoice = await prov.create_invoice(
            amount=pay_amount,
            client_ref=topup.client_ref,
            description=f"Web top-up acc{account.id}",
            notify_url=notify_url,
            success_url=settings.base_url,  # вернуться на сайт после оплаты
            network=net,
        )
    except TopupError:
        raise
    except Exception as e:  # noqa: BLE001 — провайдер недоступен/ошибка счёта
        await session.rollback()
        raise TopupError("Не удалось создать счёт. Попробуйте другой способ или позже.") from e

    topup.provider = prov.name
    topup.provider_order_id = invoice.provider_order_id
    topup.checkout_url = invoice.checkout_url
    topup.address = invoice.address
    topup.network = invoice.network
    topup.pay_amount = invoice.amount
    topup.currency = invoice.currency
    topup.expires_at = invoice.expires_at
    await session.flush()
    if not topup.checkout_url:
        raise TopupError("Платёжная система не вернула ссылку на оплату")
    return topup


async def _usdt_rate(session: AsyncSession) -> Decimal:
    """Актуальный курс USDT→₽ из БД (не из кэша бота — он живёт в другом процессе)."""
    raw = await settings_service.get(session, settings_service.USDT_RATE)
    try:
        return Decimal(raw) if raw else rates_service.DEFAULT_RATE
    except Exception:  # noqa: BLE001
        return rates_service.DEFAULT_RATE


async def get_topup(
    session: AsyncSession, account: WebAccount, topup_id: int
) -> Optional[TopUp]:
    """Вернуть топап, только если он принадлежит кошельку этого аккаунта."""
    topup = await session.get(TopUp, topup_id)
    if topup is None or topup.user_id != account.wallet_user_id:
        return None
    return topup

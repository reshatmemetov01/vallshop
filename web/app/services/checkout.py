"""Покупка товаров с баланса на сайте.

Переиспользует ``orders.purchase_from_balance`` бота: списание с баланса,
выдача из стока / заказ у поставщика, автодотяжка статусов фоновым поллером
бота (общая БД). Сайт — головной магазин (merchant_id=0), поэтому маржи нет.

Этап 1 (выбрано владельцем): «Игры и Сервисы» (ключи/гифткарты FazerCard и
свой сток) и VPN (выдаётся ссылка). Остальные типы (SMM, Steam, Звёзды,
Премиум) включим следующими итерациями — они требуют доп. полей ввода.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BotConfig
from bot.db.models import HEAD_MERCHANT_ID, OrderStatus, Variant, WebAccount
from bot.services import catalog as catalog_service
from bot.services import mprice as mprice_service
from bot.services import orders as order_service
from bot.services.pricing import owner_price_rub

from ..config import settings
from . import wallet as wallet_service

# Типы, доступные для покупки на сайте на этом этапе.
_FZR_KINDS = {"gamekey", "giftcard"}

_config: Optional[BotConfig] = None
_suppliers: Optional[dict] = None


class CheckoutError(Exception):
    """Ошибка покупки (текст пригоден для показа клиенту)."""


def _cfg() -> BotConfig:
    global _config
    if _config is None:
        _config = BotConfig.from_env()
    return _config


def _sup() -> dict:
    """Клиенты поставщиков (ленивая инициализация, кэш на процесс)."""
    global _suppliers
    if _suppliers is None:
        from fazercard import FazerCardClient
        from vpnresellers import VPNResellersClient

        _suppliers = {
            "fazercard": FazerCardClient.from_env(),
            "vpn": VPNResellersClient.from_env(),
        }
    return _suppliers


def web_buyable(variant: Variant) -> bool:
    """Можно ли купить этот номинал на сайте на текущем этапе."""
    src = variant.source or "stock"
    if src == "vpnresellers":
        return True
    if src in ("stock", "liogames"):
        return True
    if src == "fazercard":
        # Звёзды/Премиум (спец-получатель) и Steam/topup — позже.
        if variant.fzr_a in ("telegram_stars", "telegram_premium"):
            return False
        return variant.fzr_kind in _FZR_KINDS
    return False  # socproof (SMM) — позже


async def _unit_price(session: AsyncSession, variant: Variant) -> Decimal:
    owner = owner_price_rub(variant, settings.default_markup_percent)
    prices = await mprice_service.client_prices(
        session, HEAD_MERCHANT_ID, {variant.id: owner}
    )
    return Decimal(prices.get(variant.id) or 0)


async def purchase(
    session: AsyncSession, account: WebAccount, variant_id: int, quantity: int
) -> dict:
    """Купить номинал с баланса. Возвращает описание результата для фронта."""
    variant = await catalog_service.get_variant(session, variant_id)
    if variant is None:
        raise CheckoutError("Товар не найден")
    if not web_buyable(variant):
        raise CheckoutError("Покупка этого товара на сайте скоро будет доступна")

    src = variant.source or "stock"
    quantity = int(quantity or 1)
    if src == "vpnresellers":
        quantity = 1                      # VPN — одна подписка на заказ
    if quantity < 1:
        raise CheckoutError("Неверное количество")

    unit_price = await _unit_price(session, variant)
    if unit_price <= 0:
        raise CheckoutError("Цена товара недоступна")

    wallet_id = await wallet_service.ensure_wallet(session, account)
    cfg = _cfg()
    sup = _sup()

    try:
        order, items = await order_service.purchase_from_balance(
            session,
            user_id=wallet_id,
            variant=variant,
            unit_price=unit_price,
            quantity=quantity,
            merchant_id=HEAD_MERCHANT_ID,     # сайт — головной, маржи нет
            fzr=sup["fazercard"],
            vpn=sup["vpn"],
            public_base_url=cfg.public_base_url,
        )
    except order_service.InsufficientBalance:
        await session.rollback()
        raise CheckoutError("Недостаточно средств на балансе. Пополните баланс.")
    except order_service.OutOfStock:
        await session.rollback()
        raise CheckoutError("Товара нет в наличии")
    except order_service.SupplierError as e:
        await session.rollback()
        raise CheckoutError(str(e))

    await session.commit()
    return _result(variant, order, items)


def _result(variant: Variant, order, items: list) -> dict:
    status = order.status.value if isinstance(order.status, OrderStatus) else str(order.status)
    out = {
        "order_id": order.id,
        "status": status,
        "title": variant.title,
        "quantity": order.quantity,
    }
    src = variant.source or "stock"
    if src == "vpnresellers":
        out["kind"] = "vpn"
        out["link"] = items[0] if items else None
        out["message"] = "VPN готов — ссылка на подписку ниже."
    elif status == OrderStatus.COMPLETED.value and items:
        out["kind"] = "codes"
        out["codes"] = items
        out["message"] = "Готово! Ваши коды ниже."
    else:
        out["kind"] = "pending"
        out["message"] = "Заказ оформлен. Выдача появится в разделе «Мои заказы»."
    return out

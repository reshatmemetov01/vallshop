"""Покупка товаров с баланса на сайте.

Переиспользует ``orders.purchase_from_balance`` бота: списание с баланса,
выдача из стока / заказ у поставщика, автодотяжка статусов фоновым поллером
бота (общая БД). Сайт — головной магазин (merchant_id=0), поэтому маржи нет.

Этап 1 (выбрано владельцем): «Игры и Сервисы» (ключи/гифткарты FazerCard и
свой сток) и VPN (выдаётся ссылка). Остальные типы (SMM, Steam, Звёзды,
Премиум) включим следующими итерациями — они требуют доп. полей ввода.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import BotConfig
from bot.db.models import HEAD_MERCHANT_ID, OrderStatus, Variant, WebAccount
from bot.services import catalog as catalog_service
from bot.services import mprice as mprice_service
from bot.services import orders as order_service
from bot.services import rates as rates_service
from bot.services import settings as settings_service
from bot.services import smm as smm_service
from bot.services import stars as stars_service
from bot.services.pricing import owner_price_rub

from ..config import settings
from . import wallet as wallet_service

# Типы FazerCard с мгновенной выдачей кода (без доп. полей).
_FZR_KINDS = {"gamekey", "giftcard"}
# Telegram @username (после снятия ведущего @).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

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
        from socproof import SocProofClient
        from vpnresellers import VPNResellersClient

        _suppliers = {
            "fazercard": FazerCardClient.from_env(),
            "vpn": VPNResellersClient.from_env(),
            "soc": SocProofClient.from_env(),
        }
    return _suppliers


def buy_kind(variant: Variant) -> Optional[str]:
    """Как покупать номинал на сайте: simple|vpn|premium|stars; None — пока нельзя.

    * simple  — количество + фикс-цена (сток/LioGames, ключи/гифткарты FazerCard)
    * vpn     — без ввода, выдаётся ссылка
    * premium — Telegram Premium: @username, цена фиксированная
    * stars   — Telegram Звёзды: @username + количество, цена по живой котировке
    """
    src = variant.source or "stock"
    if src == "vpnresellers":
        return "vpn"
    if src in ("stock", "liogames"):
        return "simple"
    if src == "socproof":
        return "smm"
    if src == "fazercard":
        if variant.fzr_a == "telegram_stars":
            return "stars"
        if variant.fzr_a == "telegram_premium":
            return "premium"
        if variant.fzr_kind in _FZR_KINDS:
            return "simple"
    return None  # Steam/topup — позже


def web_buyable(variant: Variant) -> bool:
    return buy_kind(variant) is not None


def _smm_link(fields: Optional[dict]) -> str:
    link = ((fields or {}).get("link") or "").strip()
    if not link or " " in link or "." not in link:
        raise CheckoutError("Пришлите корректную ссылку (например https://t.me/...)")
    return link


def _username(fields: Optional[dict]) -> str:
    raw = ((fields or {}).get("telegram_username") or "").strip().lstrip("@")
    if not _USERNAME_RE.match(raw):
        raise CheckoutError("Укажите корректный Telegram @username (5–32 символа)")
    return raw


async def _db_rate(session: AsyncSession) -> Decimal:
    """Актуальный курс USDT→₽ из БД (кэш бота живёт в другом процессе)."""
    raw = await settings_service.get(session, settings_service.USDT_RATE)
    try:
        return Decimal(raw) if raw else rates_service.DEFAULT_RATE
    except Exception:  # noqa: BLE001
        return rates_service.DEFAULT_RATE


async def stars_quote(session: AsyncSession, quantity: Optional[int] = None) -> dict:
    """Границы и (если задано количество) цена для Telegram Звёзд."""
    fzr = _sup()["fazercard"]
    pps, api_min, api_max = await stars_service.get_quote(fzr)
    lo, hi = await stars_service.get_limits(session, api_min=api_min, api_max=api_max)
    out = {"min": lo, "max": hi, "price_rub": None}
    if quantity is not None:
        q = int(quantity)
        if lo <= q <= hi:
            markup = await stars_service.get_markup(session)
            rate = await _db_rate(session)
            out["price_rub"] = float(
                stars_service.price_rub(q, pps, markup=markup, our_rate=rate)
            )
    return out


async def _unit_price(session: AsyncSession, variant: Variant) -> Decimal:
    owner = owner_price_rub(variant, settings.default_markup_percent)
    prices = await mprice_service.client_prices(
        session, HEAD_MERCHANT_ID, {variant.id: owner}
    )
    return Decimal(prices.get(variant.id) or 0)


async def purchase(
    session: AsyncSession,
    account: WebAccount,
    variant_id: int,
    quantity: int,
    fields: Optional[dict] = None,
) -> dict:
    """Купить номинал с баланса. Возвращает описание результата для фронта."""
    variant = await catalog_service.get_variant(session, variant_id)
    if variant is None:
        raise CheckoutError("Товар не найден")
    kind = buy_kind(variant)
    if kind is None:
        raise CheckoutError("Покупка этого товара на сайте скоро будет доступна")

    quantity = int(quantity or 1)
    wallet_id = await wallet_service.ensure_wallet(session, account)
    cfg = _cfg()
    sup = _sup()

    # Параметры вызова покупки зависят от типа товара.
    call_quantity = quantity
    owner_unit_price: Optional[Decimal] = None
    topup_fields: Optional[dict] = None
    display_fields: Optional[list] = None
    smm_link: Optional[str] = None
    smm_quantity: Optional[int] = None

    if kind == "stars":
        uname = _username(fields)
        stars_n = quantity
        fzr = sup["fazercard"]
        pps, api_min, api_max = await stars_service.get_quote(fzr)
        lo, hi = await stars_service.get_limits(session, api_min=api_min, api_max=api_max)
        if not (lo <= stars_n <= hi):
            raise CheckoutError(f"Количество звёзд — от {lo} до {hi}")
        markup = await stars_service.get_markup(session)
        rate = await _db_rate(session)
        owner_unit_price = stars_service.price_rub(stars_n, pps, markup=markup, our_rate=rate)
        unit_price = owner_unit_price            # сайт — головной
        call_quantity = 1                         # кол-во звёзд — в полях, не в quantity
        topup_fields = {
            "telegram_username": uname,
            "stars_qty": stars_n,
            "_cost_usd": str(stars_service.cost_usd(stars_n, pps)),
        }
        display_fields = [
            {"label": "Получатель", "value": "@" + uname},
            {"label": "Количество", "value": f"{stars_n} ⭐️"},
        ]
    elif kind == "premium":
        uname = _username(fields)
        call_quantity = 1
        unit_price = await _unit_price(session, variant)
        topup_fields = {"telegram_username": uname}
        display_fields = [{"label": "Получатель", "value": "@" + uname}]
    elif kind == "smm":
        link = _smm_link(fields)
        smm_n = quantity
        err = smm_service.validate_quantity(smm_n, variant.smm_min, variant.smm_max)
        if err:
            raise CheckoutError(err)
        owner_1000 = owner_price_rub(variant, settings.default_markup_percent)
        prices = await mprice_service.client_prices(
            session, HEAD_MERCHANT_ID, {variant.id: owner_1000}
        )
        client_1000 = Decimal(prices.get(variant.id) or 0)
        unit_price = smm_service.order_total_rub(client_1000, smm_n)      # итог за весь заказ
        owner_unit_price = smm_service.order_total_rub(owner_1000, smm_n)
        call_quantity = 1
        smm_link, smm_quantity = link, smm_n
    else:
        if kind == "vpn":
            call_quantity = 1                     # VPN — одна подписка на заказ
        if call_quantity < 1:
            raise CheckoutError("Неверное количество")
        unit_price = await _unit_price(session, variant)

    if unit_price <= 0:
        raise CheckoutError("Цена товара недоступна")

    try:
        order, items = await order_service.purchase_from_balance(
            session,
            user_id=wallet_id,
            variant=variant,
            unit_price=unit_price,
            quantity=call_quantity,
            merchant_id=HEAD_MERCHANT_ID,     # сайт — головной, маржи нет
            owner_unit_price=owner_unit_price,
            fzr=sup["fazercard"],
            vpn=sup["vpn"],
            soc=sup["soc"],
            smm_link=smm_link,
            smm_quantity=smm_quantity,
            topup_fields=topup_fields,
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

    # Для Звёзд/Премиум сохраняем получателя для показа в «Мои заказы».
    if display_fields is not None:
        order.fields_json = json.dumps(display_fields, ensure_ascii=False)
        await session.flush()

    await session.commit()
    return _result(variant, order, items, kind)


def _result(variant: Variant, order, items: list, kind: str) -> dict:
    status = order.status.value if isinstance(order.status, OrderStatus) else str(order.status)
    out = {
        "order_id": order.id,
        "status": status,
        "title": variant.title,
        "quantity": order.quantity,
    }
    if kind == "vpn":
        out["kind"] = "vpn"
        out["link"] = items[0] if items else None
        out["message"] = "VPN готов — ссылка на подписку ниже."
    elif kind in ("stars", "premium"):
        out["kind"] = "pending"
        out["message"] = (
            "Заказ оформлен и отправлен поставщику. Зачисление придёт на "
            "указанный Telegram-аккаунт. Статус — в разделе «Мои заказы»."
        )
    elif kind == "smm":
        out["kind"] = "pending"
        out["message"] = (
            "Заказ оформлен. Накрутка запущена — прогресс виден в разделе "
            "«Мои заказы»."
        )
    elif status == OrderStatus.COMPLETED.value and items:
        out["kind"] = "codes"
        out["codes"] = items
        out["message"] = "Готово! Ваши коды ниже."
    else:
        out["kind"] = "pending"
        out["message"] = "Заказ оформлен. Выдача появится в разделе «Мои заказы»."
    return out

"""Жизненный цикл заказа: создание, оплата, выдача (сток + топап-фолбэк).

Схема выдачи:
  1. Основной путь — выдать код из собственного стока (мгновенно).
  2. Фолбэк — если стока нет, создать заказ у поставщика LioGames
     (`order_create`) и опрашивать статус до финала (`poll_topup`),
     учитывая лимит поставщика ~1 заказ / 60 сек.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from liogames import LioGamesClient
from fazercard import FazerCardClient, FazerCardError
from socproof import SocProofClient, SocProofError

from ..db.models import HEAD_MERCHANT_ID, Order, OrderStatus, User, Variant
from . import balance as balance_service
from . import mprice as mprice_service
from . import referral as referral_service
from . import settings as settings_service
from . import smm as smm_service
from . import smm_panels
from . import stock as stock_service

log = logging.getLogger("vallshop.orders")


class PurchaseError(Exception):
    """Базовая ошибка покупки."""


class OutOfStock(PurchaseError):
    pass


class InsufficientBalance(PurchaseError):
    pass


class SupplierError(PurchaseError):
    """Ошибка на стороне поставщика buy-on-demand (FazerCard и т.п.)."""


def _first_int(text: Optional[str]) -> Optional[int]:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None


def _no_code_delivery(variant: Variant) -> bool:
    """Доставка без кода (на аккаунт/username): топапы и Telegram Stars/Premium."""
    return variant.fzr_a in ("telegram_stars", "telegram_premium")


def new_client_ref() -> str:
    """Уникальный client_ref для идемпотентности у поставщика."""
    return "vs-" + uuid.uuid4().hex[:16]


async def ensure_user(
    session: AsyncSession, user_id: int, username: Optional[str], full_name: Optional[str],
    merchant_id: int = HEAD_MERCHANT_ID,
) -> User:
    user = await session.get(User, {"id": user_id, "merchant_id": merchant_id})
    if user is None:
        user = User(
            id=user_id, merchant_id=merchant_id, username=username, full_name=full_name
        )
        session.add(user)
        await session.flush()
    else:
        user.username = username or user.username
        user.full_name = full_name or user.full_name
    return user


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    variant: Variant,
    price_usd: Decimal,
    quantity: int = 1,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> Order:
    order = Order(
        client_ref=new_client_ref(),
        user_id=user_id,
        merchant_id=merchant_id,
        variant_id=variant.id,
        price_usd=price_usd,
        quantity=quantity,
        status=OrderStatus.CREATED,
    )
    session.add(order)
    await session.flush()
    return order


async def purchase_from_balance(
    session: AsyncSession,
    *,
    user_id: int,
    variant: Variant,
    unit_price: Decimal,
    quantity: int,
    merchant_id: int = HEAD_MERCHANT_ID,
    owner_unit_price: Optional[Decimal] = None,
    fzr: Optional[FazerCardClient] = None,
    topup_fields: Optional[dict] = None,
    vpn=None,
    public_base_url: Optional[str] = None,
    soc: Optional[SocProofClient] = None,
    panels: Optional[dict] = None,
    smm_link: Optional[str] = None,
    smm_quantity: Optional[int] = None,
) -> tuple[Order, list[str]]:
    """Купить quantity единиц с баланса и выдать коды.

    Источник выдачи зависит от ``variant.source``:
      * ``stock`` / ``liogames`` — мгновенно из собственного стока;
      * ``fazercard`` — заказ у поставщика FazerCard (buy-on-demand). Если код
        выдан сразу — заказ COMPLETED; если поставщик обрабатывает — заказ
        остаётся FULFILLING и его дотянет поллер.

    Бросает OutOfStock / InsufficientBalance / SupplierError. Вызывающий код
    коммитит при успехе и откатывает при исключении (откат возвращает баланс).
    """
    # Стоп-лосс для «умной» цены: не продаём ниже живого закупа поставщика.
    if getattr(variant, "price_mode", None) == "smart" and variant.source == "fazercard":
        from . import smartprice as smartprice_service
        try:
            if await smartprice_service.stop_loss_violation(fzr, variant, unit_price):
                raise SupplierError("Цена ниже закупа — продажа остановлена (стоп-лосс)")
        except SupplierError:
            raise
        except Exception:  # noqa: BLE001 — сбой проверки не должен ронять покупку
            pass

    total = Decimal(unit_price) * quantity

    # Маржа мерчанта: (цена клиента − цена владельца) × кол-во. Для головного
    # (owner_unit_price не передан или равен unit_price) маржа = 0.
    owner_total = Decimal(owner_unit_price if owner_unit_price is not None else unit_price) * quantity
    margin = total - owner_total
    if margin < 0:
        margin = Decimal(0)

    order = await create_order(
        session,
        user_id=user_id,
        variant=variant,
        price_usd=unit_price,
        quantity=quantity,
        merchant_id=merchant_id,
    )
    order.merchant_margin = margin

    # 1) Списываем деньги (атомарно, только если хватает).
    if not await balance_service.try_debit(session, user_id, total, merchant_id=merchant_id):
        raise InsufficientBalance()

    # Начисляем маржу на баланс заработка мерчанта в той же транзакции: если
    # выдача ниже упадёт с исключением — вызывающий откатит сессию и начисление.
    await mprice_service.accrue_earnings(session, merchant_id, margin)

    # 2) Выдача по источнику.
    if variant.source == "fazercard":
        return await _fulfill_fazercard(session, order, variant, quantity, fzr, topup_fields)

    if smm_panels.is_smm(variant.source):
        panel = smm_panels.client_for(panels, variant.source, soc)
        return await _fulfill_socproof(session, order, variant, panel, smm_link, smm_quantity)

    if variant.source == "vpnresellers":
        from . import vpn as vpn_service  # ленивый импорт (избегаем цикла)
        link = await vpn_service.fulfill(session, order, variant, vpn, public_base_url)
        await referral_service.accrue_for_order(session, order)
        return order, [link]

    # Сток (свой товар / LioGames): резервируем нужное число кодов.
    items = await stock_service.reserve_many(session, variant.id, order.id, quantity)
    if len(items) != quantity:
        raise OutOfStock()

    # Помечаем проданными и формируем выдачу.
    codes: list[str] = []
    for item in items:
        await stock_service.mark_sold(session, item)
        codes.append(item.code)

    order.delivery_code = "\n".join(codes)
    order.status = OrderStatus.COMPLETED
    await session.flush()
    await referral_service.accrue_for_order(session, order)
    return order, codes


async def _fulfill_fazercard(
    session: AsyncSession,
    order: Order,
    variant: Variant,
    quantity: int,
    fzr: Optional[FazerCardClient],
    topup_fields: Optional[dict],
) -> tuple[Order, list[str]]:
    """Оформить заказ у FazerCard. Коды — сразу или через поллер (FULFILLING)."""
    if fzr is None:
        raise SupplierError("FazerCard не настроен")

    order.supplier = "fazercard"
    order.status = OrderStatus.FULFILLING

    kind = variant.fzr_kind
    fields = topup_fields or {}

    # Steam / Звёзды: себестоимость плавающая — сохраняем её в заказ.
    if kind in ("steam", "stars") and fields.get("_cost_usd"):
        try:
            order.cost_usd = Decimal(str(fields["_cost_usd"]))
        except (InvalidOperation, ValueError, TypeError):
            pass
    await session.flush()

    def _call():
        # Telegram Stars / Premium — спец-эндпоинты с получателем (username).
        if variant.fzr_a == "telegram_stars":
            uname = (fields.get("telegram_username") or "").strip()
            # Свободный выбор количества (новый флоу) — число в fields;
            # иначе фикс-пак: количество из названия номинала.
            amount = fields.get("stars_qty") or _first_int(variant.title)
            try:
                amount = int(amount) if amount else None
            except (TypeError, ValueError):
                amount = None
            if not uname:
                raise SupplierError("Не указан Telegram @username получателя")
            if not amount:
                raise SupplierError("Не удалось определить количество звёзд")
            return fzr.order_telegram_stars(
                telegram_username=uname, quantity=amount, idempotency_key=order.client_ref,
            )
        if variant.fzr_a == "telegram_premium":
            uname = (fields.get("telegram_username") or "").strip()
            months = _first_int(variant.title)
            if not uname:
                raise SupplierError("Не указан Telegram @username получателя")
            return fzr.order_telegram_premium(
                telegram_username=uname, months=months, idempotency_key=order.client_ref,
            )
        if kind == "gamekey":
            return fzr.order_gamekey(
                game_id=variant.fzr_a, key_id=variant.fzr_b,
                quantity=quantity, idempotency_key=order.client_ref,
            )
        if kind == "giftcard":
            return fzr.order_giftcard(
                category_id=variant.fzr_a, card_id=variant.fzr_b,
                quantity=quantity, idempotency_key=order.client_ref,
            )
        if kind == "topup":
            return fzr.order_topup(
                category_id=variant.fzr_a, offer_id=variant.fzr_b,
                fields=fields, idempotency_key=order.client_ref,
            )
        if kind == "steam":
            login = (fields.get("steam_login") or "").strip()
            amount = fields.get("amount")
            currency = (fields.get("currency") or "RUB").upper()
            if not login or not amount:
                raise SupplierError("Не указан логин Steam или сумма")
            return fzr.order_steam_topup(
                steam_login=login, amount=amount, currency=currency,
                idempotency_key=order.client_ref,
            )
        raise SupplierError(f"Неизвестный тип FazerCard: {kind}")

    try:
        data = await asyncio.to_thread(_call)
    except FazerCardError as e:
        # Автовозврат включён — классическое поведение: бросаем ошибку, вызывающий
        # код откатывает списание и показывает «поставщик недоступен».
        # Выключен — деньги остаются списанными, заказ уходит в ручную обработку
        # (раздел «Не выполненные»), покупателю показываем «оформляем…».
        auto_refund = await settings_service.get_bool(
            session, settings_service.AUTO_REFUND, True
        )
        if auto_refund:
            raise SupplierError(str(e))
        order.status = OrderStatus.NEEDS_ACTION
        order.fail_reason = str(e)[:255]
        await session.flush()
        return order, []

    # order_id поставщика (для поллинга и поддержки). Разные эндпоинты кладут
    # его по-разному — ищем во всех известных ключах, иначе заказ зависнет в
    # FULFILLING (поллеру нечего опрашивать) и возврат никогда не сработает.
    env = data.get("order") if isinstance(data, dict) else None
    oid = None
    if isinstance(env, dict):
        oid = (env.get("id") or env.get("public_id") or env.get("publicId")
               or env.get("order_id"))
    if not oid and isinstance(data, dict):
        oid = data.get("order_id") or data.get("id")
    if oid:
        order.supplier_order_id = str(oid)

    codes = FazerCardClient.extract_codes(data)
    if FazerCardClient.status_is_terminal_ok(data):
        if codes:
            order.delivery_code = "\n".join(codes)
            order.status = OrderStatus.COMPLETED
        elif variant.fzr_kind in ("topup", "steam") or _no_code_delivery(variant):
            # Топап / Steam / Telegram Stars|Premium — зачисляется на аккаунт, кода нет.
            order.status = OrderStatus.COMPLETED
        # иначе (ключ/карта без кода) — остаёмся FULFILLING, коды дотянет поллер
        await session.flush()
        if order.status == OrderStatus.COMPLETED:
            await referral_service.accrue_for_order(session, order)
        return order, codes

    if FazerCardClient.status_is_terminal_failed(data):
        auto_refund = await settings_service.get_bool(
            session, settings_service.AUTO_REFUND, True
        )
        if auto_refund:
            raise SupplierError("FazerCard отклонил заказ")
        order.status = OrderStatus.NEEDS_ACTION
        order.fail_reason = "FazerCard отклонил заказ"
        await session.flush()
        return order, []

    # Обрабатывается — оставляем FULFILLING, выдачу дотянет поллер.
    await session.flush()
    return order, []


async def poll_fazercard(
    session: AsyncSession, order: Order, fzr: FazerCardClient
) -> OrderStatus:
    """Опросить статус заказа FazerCard и обновить order. Возвращает статус."""
    if not order.supplier_order_id:
        return order.status
    try:
        data = await asyncio.to_thread(fzr.get_order, order.supplier_order_id)
    except FazerCardError:
        return order.status  # сеть/поставщик недоступны — попробуем позже

    if FazerCardClient.status_is_terminal_ok(data):
        codes = FazerCardClient.extract_codes(data)
        if codes:
            order.delivery_code = "\n".join(codes)
            order.status = OrderStatus.COMPLETED
        else:
            variant = await session.get(Variant, order.variant_id)
            if variant and (variant.fzr_kind in ("topup", "steam") or _no_code_delivery(variant)):
                order.status = OrderStatus.COMPLETED  # зачислено на аккаунт/username
            else:
                order.status = OrderStatus.FAILED  # ключ/карта без кода — ошибка
    elif FazerCardClient.status_is_terminal_failed(data):
        order.status = OrderStatus.FAILED
    else:
        # Не ok и не fail — ещё в обработке. Логируем сырой статус: если это
        # новый/неизвестный терминальный статус, он виден в логах и его легко
        # добавить в SUCCESS/FAILURE_STATUSES (а не молча зависнуть навсегда).
        log.info(
            "FazerCard заказ #%s (sup=%s) ещё в обработке, статус=%r",
            order.id, order.supplier_order_id, FazerCardClient._status(data),
        )
    # иначе остаётся FULFILLING
    await session.flush()
    if order.status == OrderStatus.COMPLETED:
        await referral_service.accrue_for_order(session, order)
    return order.status


# ──────────────────────────────────────────────────────────────────────────
# SMM-поставщик soc-proof (buy-on-demand: ссылка + количество, статус поллером)
# ──────────────────────────────────────────────────────────────────────────
async def _fulfill_socproof(
    session: AsyncSession,
    order: Order,
    variant: Variant,
    soc: Optional[SocProofClient],
    link: Optional[str],
    quantity: Optional[int],
) -> tuple[Order, list[str]]:
    """Оформить SMM-заказ у soc-proof. Статус дотянет поллер (всегда FULFILLING)."""
    if soc is None or not getattr(soc, "configured", False):
        raise SupplierError("SMM-поставщик не настроен")
    qty = int(quantity or 0)
    if not variant.smm_service or not link or qty <= 0:
        raise SupplierError("Не указана услуга, ссылка или количество")

    order.supplier = variant.source        # имя панели: socproof | onexpanel
    order.status = OrderStatus.FULFILLING
    order.fields_json = json.dumps(
        {"service": int(variant.smm_service), "link": link, "quantity": qty,
         "status": "pending", "remains": qty, "progress": 0},
        ensure_ascii=False,
    )
    await session.flush()

    def _call():
        return soc.add(service=int(variant.smm_service), link=link, quantity=qty)

    try:
        data = await asyncio.to_thread(_call)
    except SocProofError as e:
        auto_refund = await settings_service.get_bool(
            session, settings_service.AUTO_REFUND, True
        )
        if auto_refund:
            raise SupplierError(str(e))
        order.status = OrderStatus.NEEDS_ACTION
        order.fail_reason = str(e)[:255]
        await session.flush()
        return order, []

    oid = None
    if isinstance(data, dict):
        oid = data.get("order") or data.get("order_id") or data.get("id")
    if not oid:
        # Без номера заказа нечего поллить и нечем возвращать — трактуем как отказ.
        auto_refund = await settings_service.get_bool(
            session, settings_service.AUTO_REFUND, True
        )
        if auto_refund:
            raise SupplierError("SMM-поставщик не вернул номер заказа")
        order.status = OrderStatus.NEEDS_ACTION
        order.fail_reason = "нет номера заказа у поставщика"
        await session.flush()
        return order, []

    order.supplier_order_id = str(oid)
    await session.flush()
    return order, []


async def settle_socproof(
    session: AsyncSession, order: Order, soc: SocProofClient
) -> dict:
    """Опросить SMM-заказ: обновить прогресс, при терминале — закрыть/вернуть.

    Возвращает ``{outcome, refund, progress, status_raw}``:
      outcome ∈ {"progress","completed","partial","canceled"}.
    Деньги возвращаются здесь же (credit + сторно маржи), статус заказа обновляется.
    """
    out = {"outcome": "progress", "refund": Decimal(0), "progress": None, "status_raw": None}
    if not order.supplier_order_id:
        return out
    try:
        data = await asyncio.to_thread(soc.status, order.supplier_order_id)
    except SocProofError:
        return out  # поставщик недоступен — попробуем позже

    raw = data.get("status") if isinstance(data, dict) else None
    remains = data.get("remains") if isinstance(data, dict) else None
    out["status_raw"] = raw

    try:
        fields = json.loads(order.fields_json) if order.fields_json else {}
    except (ValueError, TypeError):
        fields = {}
    qty = int(fields.get("quantity") or 0) or (order.quantity or 1)
    if remains is not None:
        fields["remains"] = max(0, int(float(remains)))
        fields["progress"] = smm_service.progress_percent(qty, remains)
        out["progress"] = fields["progress"]
    fields["status"] = SocProofClient.norm_status(raw)
    order.fields_json = json.dumps(fields, ensure_ascii=False)

    total_paid = Decimal(order.price_usd)
    margin = Decimal(order.merchant_margin or 0)

    if SocProofClient.is_completed(raw):
        order.status = OrderStatus.COMPLETED
        await session.flush()
        await referral_service.accrue_for_order(session, order)
        out["outcome"] = "completed"
        return out

    if SocProofClient.is_partial(raw):
        rem = max(0, min(int(float(remains or 0)), qty))
        refund = smm_service.refund_amount_rub(total_paid, qty, rem, full=False)
        if refund > 0:
            await balance_service.credit(
                session, order.user_id, refund, merchant_id=order.merchant_id
            )
            margin_back = (margin * Decimal(rem) / Decimal(qty or 1)).quantize(Decimal("0.01"))
            await mprice_service.reverse_earnings(session, order.merchant_id, margin_back)
        order.status = OrderStatus.COMPLETED
        order.fail_reason = f"частично выполнен, возврат {smm_service._dec(refund)}₽"[:255]
        await session.flush()
        out["outcome"] = "partial"
        out["refund"] = refund
        return out

    if SocProofClient.is_failed(raw):
        refund = smm_service.refund_amount_rub(total_paid, qty, qty, full=True)
        await balance_service.credit(
            session, order.user_id, refund, merchant_id=order.merchant_id
        )
        await referral_service.reverse_for_order(session, order)
        await mprice_service.reverse_earnings(session, order.merchant_id, margin)
        order.status = OrderStatus.REFUNDED
        order.fail_reason = "отменён поставщиком"
        await session.flush()
        out["outcome"] = "canceled"
        out["refund"] = refund
        return out

    await session.flush()
    return out


# ──────────────────────────────────────────────────────────────────────────
# Выдача (старый путь: сток + топап-фолбэк, оплата отдельным счётом)
# ──────────────────────────────────────────────────────────────────────────
async def fulfill_from_stock(session: AsyncSession, order: Order) -> Optional[str]:
    """Попытаться выдать код из стока. Возвращает код либо None."""
    item = await stock_service.reserve_one(session, order.variant_id, order.id)
    if item is None:
        return None
    await stock_service.mark_sold(session, item)
    order.delivery_code = item.code
    order.status = OrderStatus.COMPLETED
    await session.flush()
    return item.code


async def start_topup(
    session: AsyncSession, order: Order, variant: Variant, liog: LioGamesClient
) -> None:
    """Фолбэк: создать заказ у поставщика. client_ref = order.client_ref."""
    order.status = OrderStatus.FULFILLING
    await session.flush()
    data = liog.order_create(
        variant.liog_variation_id,
        client_ref=order.client_ref,
        product_id=variant.liog_product_id,
    )
    liog_order_id = data.get("order_id")
    if liog_order_id is not None:
        order.liog_order_id = str(liog_order_id)
    # Возможно, код уже готов сразу.
    code = LioGamesClient.extract_code(data)
    if code and LioGamesClient.status_is_terminal_ok(data):
        order.delivery_code = code
        order.status = OrderStatus.COMPLETED
    await session.flush()


async def poll_topup(
    session: AsyncSession, order: Order, liog: LioGamesClient
) -> OrderStatus:
    """Опросить статус топап-заказа и обновить order. Возвращает новый статус."""
    data = liog.order_status(
        order_id=order.liog_order_id or None,
        client_ref=order.client_ref,
    )
    if LioGamesClient.status_is_terminal_ok(data):
        code = LioGamesClient.extract_code(data)
        if code:
            order.delivery_code = code
            order.status = OrderStatus.COMPLETED
        else:
            order.status = OrderStatus.FAILED
    elif LioGamesClient.status_is_terminal_failed(data):
        order.status = OrderStatus.FAILED
    # иначе остаётся FULFILLING (PROCESSING)
    await session.flush()
    if order.status == OrderStatus.COMPLETED:
        await referral_service.accrue_for_order(session, order)
    return order.status


# ──────────────────────────────────────────────────────────────────────────
# Ручная выдача / отмена заказа администратором (раздел «Не выполненные»)
# ──────────────────────────────────────────────────────────────────────────
async def deliver_code_manual(
    session: AsyncSession, order: Order, code: str
) -> None:
    """Выдать заказ вручную с кодом: COMPLETED + сохранить код."""
    order.delivery_code = code
    order.status = OrderStatus.COMPLETED
    order.fail_reason = None
    await session.flush()
    await referral_service.accrue_for_order(session, order)


async def deliver_topup_manual(session: AsyncSession, order: Order) -> None:
    """Пометить топап вручную выполненным (без кода, зачислено на аккаунт)."""
    order.status = OrderStatus.COMPLETED
    order.fail_reason = None
    await session.flush()
    await referral_service.accrue_for_order(session, order)


async def cancel_order_manual(session: AsyncSession, order: Order) -> Decimal:
    """Отменить заказ: вернуть деньги на баланс покупателя. Возвращает сумму."""
    refund = Decimal(order.price_usd) * (order.quantity or 1)
    await balance_service.credit(session, order.user_id, refund, merchant_id=order.merchant_id)
    order.status = OrderStatus.REFUNDED
    await session.flush()
    await referral_service.reverse_for_order(session, order)
    await mprice_service.reverse_earnings(session, order.merchant_id, order.merchant_margin or Decimal(0))
    return refund


async def fulfill(
    session: AsyncSession, order: Order, variant: Variant, liog: LioGamesClient
) -> OrderStatus:
    """Полный цикл выдачи после подтверждения оплаты.

    Сначала сток, затем — топап-фолбэк. Если топап не завершился мгновенно,
    статус остаётся FULFILLING и его дотянет фоновый поллер.
    """
    code = await fulfill_from_stock(session, order)
    if code is not None:
        return OrderStatus.COMPLETED

    await start_topup(session, order, variant, liog)
    return order.status

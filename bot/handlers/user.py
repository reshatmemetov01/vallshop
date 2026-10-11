"""Пользовательские хендлеры: каталог и покупка с баланса (с количеством).

Все экраны — фото-баннер с подписью и inline-кнопками (см. bot.ui).
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..services import catalog as catalog_service
from ..services import menu as menu_service
from ..services import notify as notify_service
from ..services import referral as referral_service
from ..services import orders as order_service
from ..services import stars as stars_service
from ..services import steam as steam_service
from ..services import stock as stock_service
from ..services.balance import get_balance
from ..services.orders import InsufficientBalance, OutOfStock, SupplierError
from ..services.pricing import price_label, price_rub_value, sale_price
from ..db.models import OrderStatus
from fazercard import FazerCardClient
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router()

MAX_QTY_CAP = 50  # верхний предел количества за одну покупку


class BuyFlow(StatesGroup):
    """Покупка топапа FazerCard: пошаговый сбор данных игрока."""
    collecting = State()
    confirming = State()


class SteamFlow(StatesGroup):
    """Пополнение Steam по логину: логин → валюта → сумма → подтверждение."""
    login = State()
    currency = State()
    amount = State()
    confirming = State()


class StarsFlow(StatesGroup):
    """Telegram Звёзды: @username → количество → подтверждение."""
    username = State()
    quantity = State()
    confirming = State()


async def _set_menu_keyboard(message: Message) -> None:
    """Выставить постоянную нижнюю кнопку «🏠 Меню».

    Приём как у снятия клавиатуры: изменение reply-клавиатуры применяется к
    чату и сохраняется даже после удаления сообщения-носителя, поэтому служебное
    «⌛» сразу удаляем — чат остаётся чистым, а кнопка «Меню» остаётся внизу.
    """
    try:
        m = await message.answer("⌛", reply_markup=kb.persistent_menu_kb())
        await m.delete()
    except Exception:  # noqa: BLE001
        pass


@router.message(CommandStart())
async def cmd_start(
    message: Message, db: Database, config: BotConfig, state: FSMContext,
    command: CommandObject = None,
) -> None:
    await state.clear()
    ref_id = referral_service.parse_ref_payload(command.args if command else None)
    async with db.session() as session:
        from ..db.models import User as _User
        is_new = await session.get(_User, message.from_user.id) is None
        user = await order_service.ensure_user(
            session,
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
        )
        # Привязываем пригласившего только для новых пользователей.
        if is_new and ref_id:
            await referral_service.bind_referral(session, user, ref_id)
        await session.commit()
    await _set_menu_keyboard(message)
    is_admin = config.is_admin(message.from_user.id)
    await render(message, banner="main", caption=menu_service.caption("main", texts.START), reply_markup=kb.main_menu_kb(is_admin))


@router.message(Command("menu"))
async def cmd_menu(message: Message, config: BotConfig) -> None:
    await _set_menu_keyboard(message)
    is_admin = config.is_admin(message.from_user.id)
    await render(message, banner="main", caption=menu_service.caption("main", texts.START), reply_markup=kb.main_menu_kb(is_admin))


@router.message(F.text == "🛍 Каталог")
async def msg_catalog(message: Message, db: Database) -> None:
    async with db.session() as session:
        products = await catalog_service.list_products(session)
    caption = texts.CHOOSE_PRODUCT if products else texts.CATALOG_EMPTY
    await render(message, banner="catalog", caption=caption, reply_markup=kb.products_kb(products))


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, config: BotConfig) -> None:
    is_admin = config.is_admin(call.from_user.id)
    await render(call, banner="main", caption=menu_service.caption("main", texts.START), reply_markup=kb.main_menu_kb(is_admin))
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery, config: BotConfig, state: FSMContext) -> None:
    await state.clear()
    caption = menu_service.caption("faq", texts.FAQ)
    if not config.offer_url and config.support_contact:
        caption += texts.FAQ_NO_OFFER.format(support=config.support_contact)
    await render(
        call, banner="faq", caption=caption,
        reply_markup=kb.faq_kb(
            config.offer_url, config.agreement_url, config.privacy_url
        ),
    )
    await call.answer()


@router.callback_query(F.data == "catalog")
async def cb_catalog(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    async with db.session() as session:
        products = await catalog_service.list_products(session)
    caption = texts.CHOOSE_PRODUCT if products else texts.CATALOG_EMPTY
    # Верхний уровень — сеткой по 2 в ряд (категории-плитки).
    await render(
        call, banner="catalog", caption=caption,
        reply_markup=kb.products_kb(products, columns=2),
    )
    await call.answer()


@router.callback_query(F.data.startswith("prod:"))
async def cb_product(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    product_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await catalog_service.get_product(session, product_id)
        if not product:
            await call.answer("Товар не найден", show_alert=True)
            return
        # Спец-кнопка «Пополнить Steam» — свой флоу (логин → валюта → сумма).
        if product.game == "STEAM_TOPUP":
            await _start_steam(call, db, state)
            return
        # Спец-кнопка «Телеграм Звёзды» — свой флоу (@username → количество).
        if product.game == "TG_STARS":
            await _start_stars(call, db, state)
            return
        parent_id = product.parent_id
        children = await catalog_service.list_children(session, product_id)
        prod_emoji = texts.ce(product.icon_emoji_id or "5298953332079999355", "🎮")

        # Куда вести кнопку «Назад»: к родительской категории или в каталог.
        back = f"prod:{parent_id}" if parent_id else "catalog"

        banner_photo = product.banner_file_id or None

        if children:
            caption = f"{prod_emoji} <b>{product.title}</b>"
            if product.description:
                caption += f"\n\n{product.description}"
            caption += "\n\nВыберите категорию:"
            markup = kb.products_kb(children, back=back, back_text="Назад")
            await render(
                call, banner="catalog", caption=caption,
                reply_markup=markup, photo=banner_photo,
            )
            await call.answer()
            return

        variants = await catalog_service.list_variants(session, product_id)
        prices = {v.id: price_label(v, config.default_markup_percent) for v in variants}
        stock = await stock_service.counts_by_variant(session, [v.id for v in variants])

    if not variants:
        await call.answer("Нет доступных номиналов", show_alert=True)
        return

    # Живой сток FazerCard (под заказ — только у LioGames/своего стока).
    stock = dict(stock)
    stock.update(await catalog_service.fazercard_stock(fzr, variants))

    caption = f"{prod_emoji} <b>{product.title}</b>"
    if product.description:
        caption += f"\n\n{product.description}"
    caption += "\n\nВыберите номинал:"
    await render(
        call,
        banner="catalog",
        caption=caption,
        reply_markup=kb.variants_kb(
            variants, prices, stock, config.currency,
            product_icon=product.icon_emoji_id, back=back,
        ),
        photo=banner_photo,
    )
    await call.answer()


@router.callback_query(F.data.startswith("var:"))
async def cb_variant(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    await state.clear()  # сбросить незавершённый сбор данных топапа (в т.ч. отмену)
    variant_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, variant_id)
        if not variant or not variant.is_active:
            await call.answer("Недоступно", show_alert=True)
            return
        is_steam = variant.source == "fazercard" and variant.fzr_kind == "steam"
        is_stars = variant.source == "fazercard" and variant.fzr_kind == "stars"
    if is_steam:
        # Steam-пополнение — свой флоу (логин → валюта → сумма).
        await _start_steam(call, db, state)
        return
    if is_stars:
        await _start_stars(call, db, state)
        return
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, variant_id)
        price = sale_price(variant, config.default_markup_percent)
        in_stock = await stock_service.available_count(session, variant_id)
        ondemand = variant.source == "fazercard"
        is_vpn = variant.source == "vpnresellers"
        back = f"prod:{variant.product_id}"
        if ondemand:
            fz = await catalog_service.fazercard_stock(fzr, [variant])
            in_stock = fz.get(variant.id)  # int (реальный сток) или None (неизвестно)

    if is_vpn:
        note = "в наличии ✅ (выдача сразу после оплаты)"
    elif ondemand:
        if in_stock is None:
            note = "в наличии ✅ (выдача за пару минут)"
        elif in_stock > 0:
            note = f"в наличии: {in_stock} шт ✅"
        else:
            note = "❌ нет в наличии"
    elif in_stock > 0:
        note = f"в наличии: {in_stock} шт ✅"
    else:
        note = texts.OUT_OF_STOCK_NOTE
    caption = (
        f"<b>{variant.title}</b>\n"
        f"Цена: <b>{price_label(variant, config.default_markup_percent)}</b>\n"
        f"Статус: {note}"
    )
    await render(call, banner="catalog", caption=caption, reply_markup=kb.buy_kb(variant_id, back=back))
    await call.answer()


async def _render_quantity(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    variant_id: int, qty: int,
) -> None:
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, variant_id)
        if not variant or not variant.is_active:
            await call.answer("Недоступно", show_alert=True)
            return
        price = price_rub_value(variant, config.default_markup_percent)
        in_stock = await stock_service.available_count(session, variant_id)
        balance = await get_balance(session, call.from_user.id)
        ondemand = variant.source == "fazercard"
        is_vpn = variant.source == "vpnresellers"
        live = None
        if ondemand:
            fz = await catalog_service.fazercard_stock(fzr, [variant])
            live = fz.get(variant.id)  # int или None (неизвестно)

    if is_vpn:
        avail = 1  # VPN: один аккаунт за покупку (кол-во не выбирается)
    elif ondemand:
        # Неизвестный сток трактуем как доступный (ограничим общим капом).
        avail = MAX_QTY_CAP if live is None else live
    else:
        avail = in_stock

    if avail <= 0:
        await render(
            call, banner="catalog", caption=texts.OUT_OF_STOCK_FULL,
            reply_markup=kb.buy_kb(variant_id),
        )
        await call.answer()
        return

    max_qty = min(avail, MAX_QTY_CAP)
    qty = max(1, min(qty, max_qty))
    total = price * qty
    if ondemand:
        stock_label = "—" if live is None else live
    else:
        stock_label = in_stock

    await render(
        call,
        banner="catalog",
        caption=texts.CHOOSE_QUANTITY.format(
            item=variant.title,
            price=texts.rub(price),
            stock=stock_label,
            balance=texts.rub(balance),
        ),
        reply_markup=kb.quantity_kb(variant_id, qty, total, config.currency, max_qty),
    )
    await call.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    variant_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, variant_id)
        needs_fields = bool(variant and variant.is_active and _needs_fields(variant))
        is_steam = bool(variant and variant.source == "fazercard" and variant.fzr_kind == "steam")
        is_stars = bool(variant and variant.source == "fazercard" and variant.fzr_kind == "stars")
    if is_steam:
        await _start_steam(call, db, state)
        return
    if is_stars:
        await _start_stars(call, db, state)
        return
    if needs_fields:
        await _start_topup(call, config, fzr, state, variant)
        return
    await _render_quantity(call, db, config, fzr, variant_id, 1)


@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient
) -> None:
    _, vid, n = call.data.split(":", 2)
    await _render_quantity(call, db, config, fzr, int(vid), int(n))


def _needs_fields(variant) -> bool:
    """Нужен ли сбор данных перед покупкой (топап или Telegram Stars/Premium)."""
    return (
        variant.source == "fazercard"
        and (variant.fzr_kind == "topup"
             or variant.fzr_a in ("telegram_stars", "telegram_premium"))
    )


# ── Покупка топапа: сбор данных игрока (fields) ──────────────────────────────
async def _start_topup(call, config, fzr, state, variant) -> None:
    price = price_rub_value(variant, config.default_markup_percent)
    note = None
    if variant.fzr_a in ("telegram_stars", "telegram_premium"):
        # Telegram Stars/Premium: нужен только @username получателя.
        fields = [{"key": "telegram_username", "label": "Получатель (@username)", "type": "text"}]
        note = "Введите Telegram @username получателя — зачисление придёт на него."
    else:
        try:
            meta = await asyncio.to_thread(fzr.topup_meta, variant.fzr_a)
        except Exception:  # noqa: BLE001
            await call.answer("Поставщик недоступен, попробуйте позже", show_alert=True)
            return
        fields = meta.get("fields") or []
        note = meta.get("note")
    await state.set_state(BuyFlow.collecting)
    await state.update_data(
        vid=variant.id, fields=fields, idx=0, answers={},
        price_str=texts.rub(price), title=variant.title,
    )
    intro = f"🧩 <b>{variant.title}</b>\nЦена: <b>{texts.rub(price)}</b>"
    if note:
        intro += f"\n\nℹ️ {note}"
    await call.message.answer(intro)
    await _ask_next(call.message, state)
    await call.answer()


def _field_cancel_kb(vid: int) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"var:{vid}"))
    return b


async def _ask_next(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields, idx = data["fields"], data["idx"]
    if idx >= len(fields):
        await _show_topup_confirm(target, state)
        return
    f = fields[idx]
    label = f.get("label") or f.get("key")
    ftype = (f.get("type") or "text").lower()
    opts = f.get("options") or []
    await state.set_state(BuyFlow.collecting)
    if ftype == "select" and opts:
        b = InlineKeyboardBuilder()
        for i, o in enumerate(opts):
            b.row(InlineKeyboardButton(text=str(o), callback_data=f"fsel:{i}"))
        b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"var:{data['vid']}"))
        await target.answer(f"Выберите «{label}»:", reply_markup=b.as_markup())
    else:
        await target.answer(
            f"Введите «{label}»:", reply_markup=_field_cancel_kb(data["vid"]).as_markup()
        )


async def _show_topup_confirm(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lines = [f"🧾 <b>{data['title']}</b>", f"Цена: <b>{data['price_str']}</b>", "", "Данные:"]
    for f in data["fields"]:
        val = data["answers"].get(f["key"], "")
        lines.append(f"• {f.get('label') or f.get('key')}: <code>{val}</code>")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"✅ Купить за {data['price_str']}", callback_data="tbuy"))
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"var:{data['vid']}"))
    await state.set_state(BuyFlow.confirming)
    await target.answer("\n".join(lines), reply_markup=b.as_markup())


@router.message(BuyFlow.collecting)
async def msg_collect_field(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields, idx = data["fields"], data["idx"]
    if idx >= len(fields):
        await _show_topup_confirm(message, state)
        return
    f = fields[idx]
    if (f.get("type") or "text").lower() == "select" and (f.get("options") or []):
        await message.answer("Пожалуйста, выберите вариант кнопкой выше.")
        return
    val = (message.text or "").strip()
    if not val:
        await message.answer("Пустое значение. Введите ещё раз.")
        return
    answers = dict(data["answers"])
    answers[f["key"]] = val
    await state.update_data(answers=answers, idx=idx + 1)
    await _ask_next(message, state)


@router.callback_query(BuyFlow.collecting, F.data.startswith("fsel:"))
async def cb_select_field(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    fields, idx = data["fields"], data["idx"]
    if idx >= len(fields):
        await call.answer()
        return
    opts = fields[idx].get("options") or []
    i = int(call.data.split(":", 1)[1])
    if i >= len(opts):
        await call.answer("Список устарел", show_alert=True)
        return
    answers = dict(data["answers"])
    answers[fields[idx]["key"]] = str(opts[i])
    await state.update_data(answers=answers, idx=idx + 1)
    await call.answer()
    await _ask_next(call.message, state)


@router.callback_query(BuyFlow.confirming, F.data == "tbuy")
async def cb_topup_confirm(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    vid, answers = data["vid"], data["answers"]
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, vid)
        if not variant or not variant.is_active:
            await state.clear()
            await call.answer("Недоступно", show_alert=True)
            return
        unit_price = price_rub_value(variant, config.default_markup_percent)
        await order_service.ensure_user(
            session, call.from_user.id, call.from_user.username, call.from_user.full_name
        )
        try:
            order, codes = await order_service.purchase_from_balance(
                session, user_id=call.from_user.id, variant=variant,
                unit_price=unit_price, quantity=1, fzr=fzr, topup_fields=answers,
            )
        except InsufficientBalance:
            await session.rollback()
            await state.clear()
            await call.message.answer(
                texts.NOT_ENOUGH_BALANCE_MSG, reply_markup=kb.not_enough_balance_kb()
            )
            await call.answer()
            return
        except SupplierError:
            await session.rollback()
            await notify_service.notify_admins(
                call.message.bot, config.admin_ids,
                f"⚠️ Поставщик отклонил заказ (<b>{variant.title}</b>) — "
                "проверьте баланс у поставщика. Деньги покупателю не списаны.",
            )
            await call.answer(
                "😔 Поставщик временно недоступен, деньги не списаны.", show_alert=True
            )
            return
        # Сохраняем собранные данные (для раздела «Не выполненные» и ручной выдачи).
        collected = [
            {"label": f.get("label") or f.get("key"), "value": answers.get(f["key"], "")}
            for f in data.get("fields", [])
        ]
        order.fields_json = json.dumps(collected, ensure_ascii=False)
        status = order.status
        order_id = order.id
        pending_note = await catalog_service.resolve_text(
            session, variant, "pending_text", texts.PENDING_NOTE_DEFAULT
        )
        delivered_text = await catalog_service.resolve_text(
            session, variant, "delivered_text", texts.TOPUP_ACCOUNT_DELIVERED
        )
        await session.commit()
        total = unit_price
        balance = await get_balance(session, call.from_user.id)
        item_name = variant.title

    if status == OrderStatus.NEEDS_ACTION:
        await notify_service.notify_admins(
            call.message.bot, config.admin_ids,
            f"❗️ Заказ #{order_id} (<b>{item_name}</b>) требует ручной выдачи.\n"
            "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
        )

    await state.clear()
    bal_str = texts.rub(balance)
    total_str = texts.rub(total)
    done_kb = kb.order_done_kb(order_id, vid)
    if status == OrderStatus.COMPLETED and codes:
        codes_text = "\n".join(f"<code>{c}</code>" for c in codes)
        await call.message.answer(
            texts.PURCHASE_SUCCESS.format(
                item=item_name, qty=1, total=total_str, balance=bal_str, codes=codes_text
            ),
            reply_markup=done_kb,
        )
    elif status == OrderStatus.COMPLETED:
        if delivered_text:
            await call.message.answer(delivered_text, reply_markup=done_kb)
        else:
            await call.message.answer("✅ Заказ выполнен!", reply_markup=done_kb)
    else:  # FULFILLING
        note = f"\n\n{pending_note}" if pending_note else ""
        await call.message.answer(
            texts.PURCHASE_PENDING.format(
                item=item_name, qty=1, total=total_str, balance=bal_str, note=note
            ),
            reply_markup=kb.after_purchase_kb(),
        )
    await call.answer("Готово ✅")


# ── Пополнение Steam по логину (свободная сумма, 4 валюты) ────────────────────
def _steam_cancel_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="catalog"))
    return b.as_markup()


def _steam_currency_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    row = [
        InlineKeyboardButton(text=steam_service.CURRENCY_LABELS[c], callback_data=f"scur:{c}")
        for c in steam_service.CURRENCIES
    ]
    b.row(*row[:2])
    b.row(*row[2:])
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="catalog"))
    return b.as_markup()


def _fmt_amount(amount) -> str:
    d = Decimal(amount)
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d.normalize()}"


async def _steam_variant(session):
    from sqlalchemy import select
    from ..db.models import Product, Variant
    pid = await session.scalar(select(Product.id).where(Product.game == "STEAM_TOPUP"))
    if not pid:
        return None
    return await session.scalar(
        select(Variant)
        .where(Variant.product_id == pid, Variant.is_active.is_(True))
        .order_by(Variant.sort_order)
    )


async def _start_steam(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    async with db.session() as session:
        variant = await _steam_variant(session)
    if not variant:
        await call.answer("Пополнение Steam сейчас недоступно", show_alert=True)
        return
    await state.set_state(SteamFlow.login)
    await state.update_data(svid=variant.id)
    await call.message.answer(texts.STEAM_ASK_LOGIN, reply_markup=_steam_cancel_kb())
    await call.answer()


@router.message(SteamFlow.login)
async def msg_steam_login(
    message: Message, fzr: FazerCardClient, state: FSMContext
) -> None:
    login = (message.text or "").strip()
    if not login:
        await message.answer("Введите логин Steam")
        return
    ok = True
    try:
        res = await asyncio.to_thread(fzr.steam_check_login, login)
        if isinstance(res, dict) and "valid" in res:
            ok = bool(res.get("valid"))
    except Exception:  # noqa: BLE001 — если проверка недоступна, не блокируем
        ok = True
    if not ok:
        await message.answer(texts.STEAM_LOGIN_INVALID)
        return
    await state.update_data(login=login)
    await state.set_state(SteamFlow.currency)
    await message.answer(
        texts.STEAM_ASK_CURRENCY.format(login=login), reply_markup=_steam_currency_kb()
    )


@router.callback_query(SteamFlow.currency, F.data.startswith("scur:"))
async def cb_steam_currency(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    cur = call.data.split(":", 1)[1].upper()
    if cur not in steam_service.CURRENCIES:
        await call.answer("Валюта недоступна", show_alert=True)
        return
    data = await state.get_data()
    async with db.session() as session:
        lo, hi = await steam_service.get_limits(session)
    await state.update_data(currency=cur)
    await state.set_state(SteamFlow.amount)
    await call.message.answer(
        texts.STEAM_ASK_AMOUNT.format(
            login=data.get("login", ""), currency=cur,
            min=texts.rub(lo), max=texts.rub(hi),
        ),
        reply_markup=_steam_cancel_kb(),
    )
    await call.answer()


@router.message(SteamFlow.amount)
async def msg_steam_amount(
    message: Message, db: Database, fzr: FazerCardClient, state: FSMContext
) -> None:
    raw = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        amount = Decimal(raw)
    except Exception:  # noqa: BLE001
        amount = None
    if amount is None or amount <= 0:
        await message.answer(texts.STEAM_AMOUNT_BAD)
        return
    data = await state.get_data()
    currency, login = data.get("currency", "RUB"), data.get("login", "")
    async with db.session() as session:
        fzr_rates = await steam_service.get_fzr_rates(fzr)
        markup = await steam_service.get_markup(session)
        lo, hi = await steam_service.get_limits(session)
        balance = await get_balance(session, message.from_user.id)
    price = steam_service.price_rub(amount, currency, fzr_rates, markup=markup)
    c_usd = steam_service.cost_usd(amount, currency, fzr_rates)
    if price < lo or price > hi:
        await message.answer(
            texts.STEAM_AMOUNT_RANGE.format(
                min=texts.rub(lo), max=texts.rub(hi), price=texts.rub(price)
            )
        )
        return
    amt_str = _fmt_amount(amount)
    await state.update_data(amount=amt_str, price=str(price), cost_usd=str(c_usd))
    await state.set_state(SteamFlow.confirming)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"✅ Оплатить {texts.rub(price)}", callback_data="sbuy"))
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="catalog"))
    await message.answer(
        texts.STEAM_CONFIRM.format(
            login=login, amount=amt_str, currency=currency,
            price=texts.rub(price), balance=texts.rub(balance),
        ),
        reply_markup=b.as_markup(),
    )


@router.callback_query(SteamFlow.confirming, F.data == "sbuy")
async def cb_steam_buy(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    vid = data.get("svid")
    login, currency = data.get("login", ""), data.get("currency", "RUB")
    amount = data.get("amount", "")
    price = Decimal(data.get("price", "0"))
    cost_usd = data.get("cost_usd", "0")
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, vid)
        if not variant or not variant.is_active:
            await state.clear()
            await call.answer("Недоступно", show_alert=True)
            return
        await order_service.ensure_user(
            session, call.from_user.id, call.from_user.username, call.from_user.full_name
        )
        try:
            order, _codes = await order_service.purchase_from_balance(
                session, user_id=call.from_user.id, variant=variant,
                unit_price=price, quantity=1, fzr=fzr,
                topup_fields={
                    "steam_login": login, "amount": amount,
                    "currency": currency, "_cost_usd": cost_usd,
                },
            )
        except InsufficientBalance:
            await session.rollback()
            await state.clear()
            await call.message.answer(
                texts.NOT_ENOUGH_BALANCE_MSG, reply_markup=kb.not_enough_balance_kb()
            )
            await call.answer()
            return
        except SupplierError:
            await session.rollback()
            await notify_service.notify_admins(
                call.message.bot, config.admin_ids,
                f"⚠️ Поставщик отклонил пополнение Steam (логин <b>{login}</b>). "
                "Деньги покупателю не списаны.",
            )
            await call.answer(
                "😔 Поставщик временно недоступен, деньги не списаны.", show_alert=True
            )
            return
        order.fields_json = json.dumps(
            [
                {"label": "Логин Steam", "value": login},
                {"label": "Сумма", "value": f"{amount} {currency}"},
            ],
            ensure_ascii=False,
        )
        status = order.status
        order_id = order.id
        pending_note = await catalog_service.resolve_text(
            session, variant, "pending_text", texts.PENDING_NOTE_DEFAULT
        )
        await session.commit()
        balance = await get_balance(session, call.from_user.id)

    await state.clear()
    if status == OrderStatus.NEEDS_ACTION:
        await notify_service.notify_admins(
            call.message.bot, config.admin_ids,
            f"❗️ Пополнение Steam #{order_id} (логин <b>{login}</b>) требует ручной обработки.\n"
            "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
        )
    done_kb = kb.order_done_kb(order_id, vid)
    if status == OrderStatus.COMPLETED:
        await call.message.answer(
            texts.STEAM_DELIVERED.format(login=login, amount=amount, currency=currency),
            reply_markup=done_kb,
        )
    else:
        note = f"\n\n{pending_note}" if pending_note else ""
        await call.message.answer(
            texts.PURCHASE_PENDING.format(
                item="Пополнение Steam", qty=1, total=texts.rub(price),
                balance=texts.rub(balance), note=note,
            ),
            reply_markup=kb.after_purchase_kb(),
        )
    await call.answer("Готово ✅")


# ── Telegram Звёзды (свободный выбор количества, умная цена) ──────────────────
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _stars_cancel_kb() -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="catalog"))
    return b.as_markup()


async def _stars_variant(session):
    from sqlalchemy import select
    from ..db.models import Product, Variant
    pid = await session.scalar(select(Product.id).where(Product.game == "TG_STARS"))
    if not pid:
        return None
    return await session.scalar(
        select(Variant)
        .where(Variant.product_id == pid, Variant.is_active.is_(True))
        .order_by(Variant.sort_order)
    )


async def _start_stars(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    async with db.session() as session:
        variant = await _stars_variant(session)
    if not variant:
        await call.answer("Telegram Звёзды сейчас недоступны", show_alert=True)
        return
    await state.set_state(StarsFlow.username)
    await state.update_data(stvid=variant.id)
    await call.message.answer(texts.STARS_ASK_USERNAME, reply_markup=_stars_cancel_kb())
    await call.answer()


@router.message(StarsFlow.username)
async def msg_stars_username(
    message: Message, db: Database, fzr: FazerCardClient, state: FSMContext
) -> None:
    uname = (message.text or "").strip().lstrip("@")
    if not _USERNAME_RE.match(uname):
        await message.answer(texts.STARS_USERNAME_BAD)
        return
    async with db.session() as session:
        _pps, api_min, api_max = await stars_service.get_quote(fzr)
        lo, hi = await stars_service.get_limits(session, api_min=api_min, api_max=api_max)
    await state.update_data(username=uname)
    await state.set_state(StarsFlow.quantity)
    await message.answer(
        texts.STARS_ASK_QTY.format(username="@" + uname, min=lo, max=hi),
        reply_markup=_stars_cancel_kb(),
    )


@router.message(StarsFlow.quantity)
async def msg_stars_quantity(
    message: Message, db: Database, fzr: FazerCardClient, state: FSMContext
) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    try:
        qty = int(raw)
    except ValueError:
        qty = None
    if qty is None or qty <= 0:
        await message.answer(texts.STARS_QTY_BAD)
        return
    data = await state.get_data()
    uname = data.get("username", "")
    async with db.session() as session:
        pps, api_min, api_max = await stars_service.get_quote(fzr)
        lo, hi = await stars_service.get_limits(session, api_min=api_min, api_max=api_max)
        markup = await stars_service.get_markup(session)
        balance = await get_balance(session, message.from_user.id)
    if qty < lo or qty > hi:
        await message.answer(texts.STARS_QTY_RANGE.format(min=lo, max=hi))
        return
    price = stars_service.price_rub(qty, pps, markup=markup)
    c_usd = stars_service.cost_usd(qty, pps)
    await state.update_data(qty=qty, price=str(price), cost_usd=str(c_usd))
    await state.set_state(StarsFlow.confirming)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"✅ Оплатить {texts.rub(price)}", callback_data="stbuy"))
    b.row(InlineKeyboardButton(text="⬅️ Отмена", callback_data="catalog"))
    await message.answer(
        texts.STARS_CONFIRM.format(
            username="@" + uname, qty=qty,
            price=texts.rub(price), balance=texts.rub(balance),
        ),
        reply_markup=b.as_markup(),
    )


@router.callback_query(StarsFlow.confirming, F.data == "stbuy")
async def cb_stars_buy(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    vid = data.get("stvid")
    uname = data.get("username", "")
    qty = int(data.get("qty", 0))
    price = Decimal(data.get("price", "0"))
    cost_usd = data.get("cost_usd", "0")
    async with db.session() as session:
        variant = await catalog_service.get_variant(session, vid)
        if not variant or not variant.is_active:
            await state.clear()
            await call.answer("Недоступно", show_alert=True)
            return
        await order_service.ensure_user(
            session, call.from_user.id, call.from_user.username, call.from_user.full_name
        )
        try:
            order, _codes = await order_service.purchase_from_balance(
                session, user_id=call.from_user.id, variant=variant,
                unit_price=price, quantity=1, fzr=fzr,
                topup_fields={
                    "telegram_username": uname, "stars_qty": qty,
                    "_cost_usd": cost_usd,
                },
            )
        except InsufficientBalance:
            await session.rollback()
            await state.clear()
            await call.message.answer(
                texts.NOT_ENOUGH_BALANCE_MSG, reply_markup=kb.not_enough_balance_kb()
            )
            await call.answer()
            return
        except SupplierError:
            await session.rollback()
            await notify_service.notify_admins(
                call.message.bot, config.admin_ids,
                f"⚠️ Поставщик отклонил заказ звёзд (@{uname}, {qty} ⭐️). "
                "Деньги покупателю не списаны.",
            )
            await call.answer(
                "😔 Поставщик временно недоступен, деньги не списаны.", show_alert=True
            )
            return
        order.fields_json = json.dumps(
            [
                {"label": "Получатель", "value": "@" + uname},
                {"label": "Количество", "value": f"{qty} ⭐️"},
            ],
            ensure_ascii=False,
        )
        status = order.status
        order_id = order.id
        pending_note = await catalog_service.resolve_text(
            session, variant, "pending_text", texts.PENDING_NOTE_DEFAULT
        )
        await session.commit()
        balance = await get_balance(session, call.from_user.id)

    await state.clear()
    if status == OrderStatus.NEEDS_ACTION:
        await notify_service.notify_admins(
            call.message.bot, config.admin_ids,
            f"❗️ Заказ звёзд #{order_id} (@{uname}, {qty} ⭐️) требует ручной обработки.\n"
            "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
        )
    done_kb = kb.order_done_kb(order_id, vid)
    if status == OrderStatus.COMPLETED:
        await call.message.answer(
            texts.STARS_DELIVERED.format(username="@" + uname, qty=qty),
            reply_markup=done_kb,
        )
    else:
        note = f"\n\n{pending_note}" if pending_note else ""
        await call.message.answer(
            texts.PURCHASE_PENDING.format(
                item="Telegram Звёзды", qty=1, total=texts.rub(price),
                balance=texts.rub(balance), note=note,
            ),
            reply_markup=kb.after_purchase_kb(),
        )
    await call.answer("Готово ✅")


@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient, vpn=None
) -> None:
    _, vid, n = call.data.split(":", 2)
    variant_id, qty = int(vid), int(n)

    async with db.session() as session:
        variant = await catalog_service.get_variant(session, variant_id)
        if not variant or not variant.is_active:
            await call.answer("Недоступно", show_alert=True)
            return
        is_vpn = variant.source == "vpnresellers"
        if is_vpn:
            qty = 1
        unit_price = price_rub_value(variant, config.default_markup_percent)
        await order_service.ensure_user(
            session,
            user_id=call.from_user.id,
            username=call.from_user.username,
            full_name=call.from_user.full_name,
        )
        try:
            order, codes = await order_service.purchase_from_balance(
                session,
                user_id=call.from_user.id,
                variant=variant,
                unit_price=unit_price,
                quantity=qty,
                fzr=fzr,
                vpn=vpn,
                public_base_url=config.public_base_url,
            )
        except InsufficientBalance:
            await session.rollback()
            await call.message.answer(
                texts.NOT_ENOUGH_BALANCE_MSG, reply_markup=kb.not_enough_balance_kb()
            )
            await call.answer()
            return
        except OutOfStock:
            await session.rollback()
            in_stock = await stock_service.available_count(session, variant_id)
            await call.answer(
                texts.NOT_ENOUGH_STOCK.format(stock=in_stock), show_alert=True
            )
            return
        except SupplierError:
            await session.rollback()
            await notify_service.notify_admins(
                call.message.bot, config.admin_ids,
                f"⚠️ Поставщик отклонил заказ (<b>{variant.title}</b>) — "
                "проверьте баланс у поставщика. Деньги покупателю не списаны.",
            )
            await call.answer(
                "😔 Поставщик временно недоступен, деньги не списаны. "
                "Попробуйте позже.",
                show_alert=True,
            )
            return

        pending = order.status in (
            OrderStatus.FULFILLING, OrderStatus.NEEDS_ACTION
        ) and not codes
        needs_action = order.status == OrderStatus.NEEDS_ACTION
        order_id = order.id
        pending_note = await catalog_service.resolve_text(
            session, variant, "pending_text", texts.PENDING_NOTE_DEFAULT
        )
        await session.commit()
        total = unit_price * qty
        balance = await get_balance(session, call.from_user.id)
        item_name = variant.title

    if needs_action:
        await notify_service.notify_admins(
            call.message.bot, config.admin_ids,
            f"❗️ Заказ #{order_id} (<b>{item_name}</b>) требует ручной выдачи.\n"
            "Откройте «Админ-панель → Текущие заказы → Не выполненные».",
        )

    if pending:
        note = f"\n\n{pending_note}" if pending_note else ""
        await render(
            call,
            banner="catalog",
            caption=texts.PURCHASE_PENDING.format(
                item=item_name,
                qty=qty,
                total=texts.rub(total),
                balance=texts.rub(balance),
                note=note,
            ),
            reply_markup=kb.after_purchase_kb(),
        )
        await call.answer("Оформляем ⏳")
        return

    if is_vpn:
        # VPN: выдаём ссылку-подписку (кликабельна) + инструкцию по подключению.
        link = codes[0] if codes else (order.delivery_code or "")
        await call.message.answer(
            texts.VPN_DELIVERED.format(link=link),
            reply_markup=kb.order_done_kb(order_id, variant_id),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await call.answer("Готово ✅")
        return

    codes_text = "\n".join(f"<code>{c}</code>" for c in codes)
    await render(
        call,
        banner="catalog",
        caption=texts.PURCHASE_SUCCESS.format(
            item=item_name,
            qty=qty,
            total=texts.rub(total),
            balance=texts.rub(balance),
            codes=codes_text,
        ),
        reply_markup=kb.order_done_kb(order_id, variant_id),
    )
    await call.answer("Готово ✅")

"""Админ-раздел «Текущие заказы».

Настройки автовозврата и редактируемые тексты покупателю + раздел
«Не выполненные» для ручной выдачи/отмены заказов, которые магазин не смог
оформить автоматически (ошибка поставщика / нет средств у поставщика).
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import Order, OrderStatus, Variant
from ..services import catalog as catalog_service
from ..services import notify as notify_service
from ..services import orders as order_service
from ..services import settings as settings_service
from .. import texts

log = logging.getLogger("vallshop.admin_orders")

router = Router(name="admin_orders")


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class COState(StatesGroup):
    set_refund = State()
    set_cancel = State()
    send_code = State()


# ── Названия статусов для админа ────────────────────────────────────────────
_STATUS_RU = {
    OrderStatus.NEEDS_ACTION: "⏳ ждёт ручной выдачи",
    OrderStatus.FAILED: "⚠️ ошибка выдачи",
    OrderStatus.FULFILLING: "⏳ оформляется",
}

# Статусы, попадающие в «Не выполненные».
_PENDING_STATUSES = (OrderStatus.NEEDS_ACTION, OrderStatus.FAILED)


def _btn(text: str, data: str):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Отмена", back))
    return kb.as_markup()


# ── Панель «Текущие заказы» ─────────────────────────────────────────────────
async def _home_kb(session) -> tuple[str, InlineKeyboardMarkup]:
    auto = await settings_service.get_bool(session, settings_service.AUTO_REFUND, True)
    cnt = int(
        await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_(_PENDING_STATUSES))
        )
        or 0
    )
    state = "🟢 ВКЛ" if auto else "🔴 ВЫКЛ"
    caption = (
        "🛒 <b>Текущие заказы</b>\n\n"
        f"Автовозврат денег при провале: <b>{state}</b>\n"
        + ("При ошибке деньги возвращаются покупателю автоматически.\n"
           if auto else
           "При ошибке деньги остаются списанными, заказ уходит в "
           "«Не выполненные» для ручной выдачи.\n")
        + f"\nНе выполненных заказов: <b>{cnt}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"Автовозврат: {state} (переключить)", "co_toggle"))
    kb.row(
        _btn("✏️ Текст возврата", "co_txt:refund"),
        _btn("✏️ Текст отмены", "co_txt:cancel"),
    )
    kb.row(_btn(f"❗ Не выполненные ({cnt})", "co_list"))
    kb.row(_btn("⬅️ Назад", "a_home"))
    return caption, kb.as_markup()


@router.callback_query(F.data == "co_home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    async with db.session() as session:
        caption, markup = await _home_kb(session)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "co_toggle")
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    async with db.session() as session:
        auto = await settings_service.get_bool(session, settings_service.AUTO_REFUND, True)
        await settings_service.set(
            session, settings_service.AUTO_REFUND, "0" if auto else "1"
        )
        await session.commit()
        caption, markup = await _home_kb(session)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Переключено")


# ── Редактирование текстов (возврат / отмена) ───────────────────────────────
_TXT = {
    "refund": (settings_service.REFUND_TEXT, texts.FULFILL_REFUNDED,
               "текст автовозврата", COState.set_refund),
    "cancel": (settings_service.CANCEL_TEXT, texts.CANCEL_TEXT_DEFAULT,
               "текст отмены", COState.set_cancel),
}


@router.callback_query(F.data.startswith("co_txt:"))
async def cb_edit_text(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    which = call.data.split(":", 1)[1]
    key, default, label, st = _TXT[which]
    async with db.session() as session:
        cur = await settings_service.get(session, key, default)
    await state.set_state(st)
    await call.message.edit_text(
        f"✏️ Редактирование: <b>{label}</b>\n"
        f"сейчас:\n<blockquote>{cur}</blockquote>\n\n"
        "Пришлите новый текст. Можно премиум-эмодзи и форматирование.\n"
        "Плейсхолдеры <code>{ref}</code> (номер заказа) и <code>{amount}</code> "
        "(сумма) подставятся автоматически, если вставите их.\n"
        "<code>0</code> — вернуть текст по умолчанию.",
        reply_markup=_cancel_kb("co_home"),
    )
    await call.answer()


async def _save_text(message: Message, db: Database, state: FSMContext, which: str) -> None:
    key, default, label, _ = _TXT[which]
    plain = (message.text or "").strip()
    if not plain:
        await message.answer("Пусто. Пришлите текст или 0 (по умолчанию).")
        return
    if plain == "0":
        value, note = default, "сброшен к значению по умолчанию"
    else:
        value, note = (message.html_text or message.text).strip(), "обновлён"
    async with db.session() as session:
        await settings_service.set(session, key, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К текущим заказам", "co_home"))
    await message.answer(f"✅ {label.capitalize()} {note}", reply_markup=kb.as_markup())


@router.message(COState.set_refund)
async def msg_set_refund(message: Message, db: Database, state: FSMContext) -> None:
    await _save_text(message, db, state, "refund")


@router.message(COState.set_cancel)
async def msg_set_cancel(message: Message, db: Database, state: FSMContext) -> None:
    await _save_text(message, db, state, "cancel")


# ── Список «Не выполненные» ─────────────────────────────────────────────────
@router.callback_query(F.data == "co_list")
async def cb_list(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    async with db.session() as session:
        orders = list(
            await session.scalars(
                select(Order)
                .where(Order.status.in_(_PENDING_STATUSES))
                .order_by(Order.created_at.desc())
                .limit(30)
            )
        )
        rows = []
        for o in orders:
            v = await session.get(Variant, o.variant_id)
            rows.append((o.id, v.title if v else "—", o.status))
    kb = InlineKeyboardBuilder()
    for oid, title, st in rows:
        mark = "⏳" if st == OrderStatus.NEEDS_ACTION else "⚠️"
        kb.row(_btn(f"{mark} #{oid} · {title}", f"co_ord:{oid}"))
    kb.row(_btn("⬅️ Назад", "co_home"))
    caption = (
        "❗ <b>Не выполненные заказы</b>\nВыберите заказ для ручной обработки:"
        if rows else "❗ <b>Не выполненные заказы</b>\n\nПусто — всё выдано ✅"
    )
    await call.message.edit_text(caption, reply_markup=kb.as_markup())
    await call.answer()


def _fields_lines(order: Order) -> str:
    if not order.fields_json:
        return ""
    try:
        data = json.loads(order.fields_json)
    except Exception:  # noqa: BLE001
        return ""
    out = []
    for item in data:
        label = item.get("label", "")
        value = item.get("value", "")
        out.append(f"• {label}: <code>{value}</code>")
    return "\n".join(out)


async def _order_detail(session, order_id: int) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    order = await session.get(Order, order_id)
    if not order:
        return None, None
    variant = await catalog_service.get_variant(session, order.variant_id)
    title = variant.title if variant else "—"
    product_title = "—"
    if variant and variant.product is not None:
        product_title = variant.product.title
    when = order.created_at.strftime("%d.%m.%Y %H:%M") if order.created_at else "—"
    st_ru = _STATUS_RU.get(order.status, order.status.value if hasattr(order.status, "value") else str(order.status))
    lines = [
        f"🧾 <b>Заказ #{order.id}</b>",
        f"Статус: {st_ru}",
        f"Категория/товар: <b>{product_title}</b>",
        f"Номинал: <b>{title}</b>",
        f"Количество: <b>{order.quantity}</b>",
        f"Сумма: <b>{texts.rub(Decimal(order.price_usd) * (order.quantity or 1))}</b>",
        f"Оформлен: {when}",
    ]
    if order.supplier_order_id:
        lines.append(f"ID у поставщика: <code>{order.supplier_order_id}</code>")
    fields = _fields_lines(order)
    if fields:
        lines.append("\n<b>Данные покупателя:</b>")
        lines.append(fields)
    if order.fail_reason:
        lines.append(f"\nПричина: <i>{order.fail_reason}</i>")
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✅ Выдать товар вручную", f"co_fulfill:{order.id}"))
    kb.row(_btn("🚫 Отмена (вернуть деньги)", f"co_cancel:{order.id}"))
    kb.row(_btn("⬅️ Назад", "co_list"))
    return "\n".join(lines), kb.as_markup()


@router.callback_query(F.data.startswith("co_ord:"))
async def cb_order(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    oid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        caption, markup = await _order_detail(session, oid)
    if not caption:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


# ── Ручная выдача ───────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("co_fulfill:"))
async def cb_fulfill(call: CallbackQuery, db: Database) -> None:
    oid = int(call.data.split(":", 1)[1])
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📨 Отправить код", f"co_code:{oid}"))
    kb.row(_btn("💠 Отправить пополнение", f"co_topup:{oid}"))
    kb.row(_btn("⬅️ Назад", f"co_ord:{oid}"))
    await call.message.edit_text(
        "Как выдать заказ вручную?\n"
        "• <b>Отправить код</b> — пришлёте код, он уйдёт покупателю.\n"
        "• <b>Отправить пополнение</b> — сами пополняете по данным ниже, "
        "затем жмёте «Пополнил».",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("co_code:"))
async def cb_code_ask(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":", 1)[1])
    await state.set_state(COState.send_code)
    await state.update_data(oid=oid)
    await call.message.edit_text(
        "📨 Пришлите <b>код</b>, который отправить покупателю.\n"
        "(Несколько кодов — каждый с новой строки.)",
        reply_markup=_cancel_kb(f"co_ord:{oid}"),
    )
    await call.answer()


@router.message(COState.send_code)
async def msg_send_code(message: Message, db: Database, config: BotConfig, state: FSMContext) -> None:
    data = await state.get_data()
    oid = data.get("oid")
    code = (message.text or "").strip()
    if not code:
        await message.answer("Пусто. Пришлите код.")
        return
    async with db.session() as session:
        order = await session.get(Order, oid)
        if not order:
            await state.clear()
            await message.answer("Заказ не найден")
            return
        await order_service.deliver_code_manual(session, order, code)
        user_id = order.user_id
        await session.commit()
    # Сообщение покупателю.
    try:
        await message.bot.send_message(user_id, texts.DELIVERY_SUCCESS.format(code=code))
    except Exception:  # noqa: BLE001
        log.warning("Не удалось отправить код покупателю %s", user_id)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К не выполненным", "co_list"))
    await message.answer("✅ Код отправлен покупателю, заказ выполнен.", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("co_topup:"))
async def cb_topup_show(call: CallbackQuery, db: Database) -> None:
    oid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        order = await session.get(Order, oid)
        if not order:
            await call.answer("Не найдено", show_alert=True)
            return
        fields = _fields_lines(order)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✅ Пополнил", f"co_toppaid:{oid}"))
    kb.row(_btn("⬅️ Назад", f"co_ord:{oid}"))
    body = fields or "— (данные не сохранены)"
    await call.message.edit_text(
        "💠 <b>Ручное пополнение</b>\nПополните по данным покупателя:\n\n"
        f"{body}\n\n"
        "После пополнения нажмите «Пополнил» — покупателю уйдёт сообщение об успехе.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("co_toppaid:"))
async def cb_topup_paid(call: CallbackQuery, db: Database) -> None:
    oid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        order = await session.get(Order, oid)
        if not order:
            await call.answer("Не найдено", show_alert=True)
            return
        variant = await session.get(Variant, order.variant_id)
        delivered_text = texts.TOPUP_ACCOUNT_DELIVERED
        if variant is not None and getattr(variant, "fzr_a", None) == "telegram_stars":
            # Звёзды — отдельный текст с получателем и количеством.
            uname, qty = order_service.stars_recipient_qty(order)
            delivered_text = (
                texts.STARS_DELIVERED_MANUAL.format(username=uname, qty=qty)
                if uname and qty else texts.TOPUP_ACCOUNT_DELIVERED
            )
        elif variant is not None:
            delivered_text = await catalog_service.resolve_text(
                session, variant, "delivered_text", texts.TOPUP_ACCOUNT_DELIVERED
            )
        await order_service.deliver_topup_manual(session, order)
        user_id = order.user_id
        await session.commit()
    if delivered_text:
        try:
            await call.message.bot.send_message(user_id, delivered_text)
        except Exception:  # noqa: BLE001
            log.warning("Не удалось уведомить покупателя %s", user_id)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К не выполненным", "co_list"))
    await call.message.edit_text(
        "✅ Пополнение отмечено выполненным, покупатель уведомлён.",
        reply_markup=kb.as_markup(),
    )
    await call.answer("Готово")


@router.callback_query(F.data.startswith("co_cancel:"))
async def cb_cancel(call: CallbackQuery, db: Database) -> None:
    oid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        order = await session.get(Order, oid)
        if not order:
            await call.answer("Не найдено", show_alert=True)
            return
        ref = order.client_ref
        refund = await order_service.cancel_order_manual(session, order)
        user_id = order.user_id
        cancel_text = await settings_service.get(
            session, settings_service.CANCEL_TEXT, texts.CANCEL_TEXT_DEFAULT
        )
        await session.commit()
    msg = settings_service.fmt(
        cancel_text or texts.CANCEL_TEXT_DEFAULT, ref=ref, amount=texts.rub(refund)
    )
    try:
        await call.message.bot.send_message(user_id, msg)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось уведомить покупателя %s об отмене", user_id)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К не выполненным", "co_list"))
    await call.message.edit_text(
        f"🚫 Заказ отменён, покупателю возвращено <b>{texts.rub(refund)}</b>.",
        reply_markup=kb.as_markup(),
    )
    await call.answer("Отменено")

"""Активация заранее оплаченных кодов (Robux через гейм-пасс).

Флоу: кнопка «Активировать Код» → ввод кода → подтверждение создания пасса →
ввод ника → подтверждение снятой Regional Pricing → заявка админу с
авто-отчётом Roblox и кнопками ✅/❌. Оплаты в этой ветке нет.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import roblox

from ..config import BotConfig
from ..db import Database
from ..db.models import ActivationRequest, HEAD_MERCHANT_ID
from ..services import activation as act
from ..services import menu as menu_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

log = logging.getLogger("vallshop.activation")
router = Router()

_NOPREV = LinkPreviewOptions(is_disabled=True)
_CODE_RE = re.compile(r"\d{3}-\d{3}-\d{3}")


class ActivateFlow(StatesGroup):
    code = State()
    confirm_pass = State()
    nickname = State()


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_b("⬅️ Отмена", "act_cancel"))
    return b.as_markup()


def _admin_decision_kb(rid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_b("✅ Успешно", f"aok:{rid}"), _b("❌ Ошибка", f"aerr:{rid}"))
    return b.as_markup()


def _reasons_kb(rid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (short, _tpl) in enumerate(texts.GAMEPASS_REASONS):
        b.row(_b(f"❌ {short}", f"ar:{i}:{rid}"))
    b.row(_b("⬅️ Назад", f"aback:{rid}"))
    return b.as_markup()


# ── Пользовательский флоу ────────────────────────────────────────────────────
@router.callback_query(F.data == "activate")
async def cb_activate(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ActivateFlow.code)
    await call.message.answer(texts.ACT_ASK_CODE, reply_markup=_cancel_kb())
    await call.answer()


@router.callback_query(F.data == "act_cancel")
async def cb_act_cancel(
    call: CallbackQuery, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    is_admin = config.is_admin(call.from_user.id)
    await render(call, banner="main", caption=menu_service.caption("main", texts.START, merchant_id), reply_markup=kb.main_menu_kb(is_admin, merchant_id=merchant_id))
    await call.answer("Отменено")


@router.message(ActivateFlow.code)
async def msg_code(message: Message, db: Database, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not _CODE_RE.fullmatch(code):
        await message.answer(texts.ACT_BAD_FORMAT, reply_markup=_cancel_kb())
        return
    async with db.session() as s:
        c = await act.find_code(s, code)
        if not c:
            await state.clear()
            await message.answer(texts.ACT_NOT_FOUND)
            return
        if c.status == "used":
            await state.clear()
            await message.answer(texts.ACT_USED)
            return
        product = c.product
        instruction = c.instruction or "—"
    expected = roblox.expected_gamepass_price(roblox.parse_robux_amount(product))
    price_line = texts.ACT_PRICE_LINE.format(price=expected) if expected else ""
    await state.update_data(code=code, product=product)
    await state.set_state(ActivateFlow.confirm_pass)
    b = InlineKeyboardBuilder()
    b.row(_b("✅ Гейм Пасс Создан", "act_passdone"))
    b.row(_b("⬅️ Отмена", "act_cancel"))
    await message.answer(
        texts.ACT_CODE_FOUND.format(
            product=product, instruction=instruction, price_line=price_line
        ),
        reply_markup=b.as_markup(), link_preview_options=_NOPREV,
    )


@router.callback_query(ActivateFlow.confirm_pass, F.data == "act_passdone")
async def cb_passdone(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ActivateFlow.nickname)
    await call.message.answer(
        texts.ACT_ASK_NICK, reply_markup=_cancel_kb(), link_preview_options=_NOPREV
    )
    await call.answer()


@router.message(ActivateFlow.nickname)
async def msg_nick(
    message: Message, db: Database, config: BotConfig, state: FSMContext
) -> None:
    nick = (message.text or "").strip()
    if not nick:
        await message.answer("Пришлите ник.", reply_markup=_cancel_kb())
        return

    data = await state.get_data()
    code = data.get("code")
    product = data.get("product")
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "—"

    async with db.session() as s:
        c = await act.find_code(s, code)
        if not c or c.status == "used":
            await state.clear()
            await message.answer(texts.ACT_USED)
            return
        await act.mark_used(s, code, user_id)
        robux = roblox.parse_robux_amount(product)
        expected = roblox.expected_gamepass_price(robux)
        req = await act.create_request(
            s, code=code, product=product, user_id=user_id, username=username,
            nickname=nick, expected_price=expected, actual_price=None,
        )
        await s.commit()
        rid = req.id

    await state.clear()
    await message.answer(
        texts.ACT_SUBMITTED.format(nickname=nick, product=product, code=code)
    )

    # Авто-отчёт Roblox (может занять несколько секунд) → затем уведомление админам.
    try:
        report, _uid, actual = await asyncio.to_thread(
            roblox.build_gamepass_report, nick, expected, robux
        )
    except Exception:  # noqa: BLE001
        report, actual = "⚠️ Не удалось получить данные Roblox (проверьте вручную).", None

    if actual is not None:
        async with db.session() as s:
            r = await s.get(ActivationRequest, rid)
            if r:
                r.actual_price = actual
                await s.commit()

    admin_text = (
        "📢 <b>Новая заявка на активацию!</b>\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"💬 {username}\n"
        f"🆔 ID: {user_id}\n\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"📦 Товар: {product}\n"
        f"🎮 Ник: {nick}\n\n"
        f"{report}"
    )
    if len(admin_text) > 4000:
        admin_text = admin_text[:3950] + "\n\n… (обрезано)"

    for admin_id in config.admin_ids:
        try:
            await message.bot.send_message(
                admin_id, admin_text, reply_markup=_admin_decision_kb(rid),
                link_preview_options=_NOPREV,
            )
        except Exception:  # noqa: BLE001
            log.warning("Не удалось отправить заявку админу %s", admin_id)


# ── Решение админа ───────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("aerr:"))
async def cb_admin_err(call: CallbackQuery, config: BotConfig) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    await call.message.edit_reply_markup(reply_markup=_reasons_kb(rid))
    await call.answer("Выберите причину")


@router.callback_query(F.data.startswith("aback:"))
async def cb_admin_back(call: CallbackQuery, config: BotConfig) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    await call.message.edit_reply_markup(reply_markup=_admin_decision_kb(rid))
    await call.answer()


@router.callback_query(F.data.startswith("aok:"))
async def cb_admin_ok(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    async with db.session() as s:
        req = await act.get_request(s, rid)
        if not req:
            await call.answer("Заявка не найдена", show_alert=True)
            return
        uid = req.user_id
        await act.decide_request(s, rid, "approved")
        await s.commit()
    try:
        await call.bot.send_message(uid, texts.GAMEPASS_OK_TEXT, link_preview_options=_NOPREV)
    except Exception as e:  # noqa: BLE001
        await call.answer(f"Не отправлено пользователю: {e}", show_alert=True)
        return
    await call.message.edit_text(
        (call.message.text or "") + "\n\n✅ ОДОБРЕНО — пользователю отправлено уведомление.",
        link_preview_options=_NOPREV,
    )
    await call.answer("✅ Одобрено")


@router.callback_query(F.data.startswith("ar:"))
async def cb_admin_reason(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    if not config.is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    _, idx_s, rid_s = call.data.split(":")
    idx, rid = int(idx_s), int(rid_s)
    short, template = texts.GAMEPASS_REASONS[idx]
    async with db.session() as s:
        req = await act.get_request(s, rid)
        if not req:
            await call.answer("Заявка не найдена", show_alert=True)
            return
        uid = req.user_id
        code = req.code
        expected = req.expected_price
        actual = req.actual_price
        await act.set_free(s, code)  # вернуть код в свободные для повторной попытки
        await act.decide_request(s, rid, "rejected")
        await s.commit()
    user_text = template.format(
        expected=expected if expected is not None else "нужную",
        actual=actual if actual is not None else "—",
    )
    try:
        await call.bot.send_message(uid, user_text, link_preview_options=_NOPREV)
    except Exception as e:  # noqa: BLE001
        await call.answer(f"Не отправлено пользователю: {e}", show_alert=True)
        return
    await call.message.edit_text(
        (call.message.text or "") + f"\n\n❌ ОТКЛОНЕНО ({short}). Код возвращён в свободные.",
        link_preview_options=_NOPREV,
    )
    await call.answer("❌ Отклонено")

"""Инлайн-раздел админ-панели «Коды активации».

Только для ADMIN_IDS. Функции (инлайн-кнопки + ввод одним сообщением):
добавить коды списком, статус кода, сменить статус, Roblox-ник,
блокировки/пользователи, личное сообщение, рассылка.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter
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
from sqlalchemy import func, select

import roblox

from ..config import BotConfig
from ..db import Database
from ..db.models import ActivationCode, User
from ..services import activation as act
from ..services.users import get_user
from roblox import parse_robux_amount

log = logging.getLogger("vallshop.admincodes")
router = Router()
_NOPREV = LinkPreviewOptions(is_disabled=True)


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class AC(StatesGroup):
    addcodes = State()
    status = State()
    setstatus = State()
    roblox = State()
    block = State()
    unblock = State()
    addusers = State()
    msg = State()
    broadcast = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _back_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("⬅️ Назад", "ac_home"))
    return b.as_markup()


def _panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📥 Добавить коды", "ac_addcodes"))
    kb.row(_btn("🔎 Статус кода", "ac_status"), _btn("✏️ Сменить статус", "ac_setstatus"))
    kb.row(_btn("🎮 Roblox ник", "ac_roblox"))
    kb.row(_btn("🚫 Заблокировать", "ac_block"), _btn("✅ Разблокировать", "ac_unblock"))
    kb.row(_btn("📋 Блокировки", "ac_blocked"), _btn("👥 Пользователей", "ac_usercount"))
    kb.row(_btn("➕ Пользователи", "ac_addusers"))
    kb.row(_btn("✉️ Сообщение", "ac_msg"), _btn("📣 Рассылка", "ac_broadcast"))
    kb.row(_btn("⬅️ В админ-панель", "a_home"))
    return kb.as_markup()


async def _show_panel(call: CallbackQuery, db: Database) -> None:
    async with db.session() as s:
        c = await act.code_counts(s)
        users = int(await s.scalar(select(func.count()).select_from(User)) or 0)
    text = (
        "🎟 <b>Коды активации</b>\n\n"
        f"Кодов: <b>{c['total']}</b> (свободно {c['free']} / использовано {c['used']})\n"
        f"Пользователей: <b>{users}</b>\n\n"
        "Выберите действие:"
    )
    try:
        await call.message.edit_text(text, reply_markup=_panel_kb())
    except Exception:  # noqa: BLE001
        await call.message.answer(text, reply_markup=_panel_kb())


@router.callback_query(F.data == "ac_home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_panel(call, db)
    await call.answer()


# ── Коды ──────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "ac_addcodes")
async def cb_addcodes(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.addcodes)
    await call.message.edit_text(
        "📥 <b>Добавить коды списком</b>\n\nПришлите одним сообщением:\n"
        "<code>Товар</code> (первая строка)\n<code>инструкция…</code>\n"
        "<i>(пустая строка)</i>\n<code>код1</code>\n<code>код2</code>\n…",
        reply_markup=_back_kb(),
    )
    await call.answer()


@router.message(AC.addcodes)
async def msg_addcodes(message: Message, db: Database, state: FSMContext) -> None:
    lines = (message.text or "").splitlines()
    if len(lines) < 2:
        await message.answer("Формат: Товар ↵ инструкция ↵ (пустая) ↵ коды.")
        return
    product = lines[0].strip()
    if not product:
        await message.answer("Укажите товар в первой строке.")
        return
    instruction_lines, code_lines, found_empty = [], [], False
    for ln in lines[1:]:
        s = ln.strip()
        if not s and not found_empty:
            found_empty = True
            continue
        if not found_empty:
            instruction_lines.append(s)
        elif s:
            code_lines.append(s)
    instruction = "\n".join(instruction_lines) or None
    if not code_lines:
        await message.answer("Не указаны коды (после пустой строки).")
        return
    added = dup = 0
    async with db.session() as s:
        for code in code_lines:
            ok = await act.add_code(
                s, code=code, product=product, instruction=instruction,
                robux_amount=parse_robux_amount(product),
            )
            added += 1 if ok else 0
            dup += 0 if ok else 1
        await s.commit()
    await state.clear()
    await message.answer(
        f"✅ Добавлено: {added}" + (f", пропущено дублей: {dup}" if dup else "")
        + f"\n📦 Товар: {product}",
        reply_markup=_back_kb(),
    )


@router.callback_query(F.data == "ac_status")
async def cb_status(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.status)
    await call.message.edit_text("🔎 Пришлите код (XXX-XXX-XXX):", reply_markup=_back_kb())
    await call.answer()


@router.message(AC.status)
async def msg_status(message: Message, db: Database, state: FSMContext) -> None:
    code = (message.text or "").strip()
    async with db.session() as s:
        c = await act.find_code(s, code)
    await state.clear()
    if not c:
        await message.answer("🚫 Код не найден.", reply_markup=_back_kb())
        return
    await message.answer(
        f"🔑 <code>{c.code}</code>\n📦 {c.product}\nСтатус: <b>{c.status}</b>\n"
        f"Кем использован: {c.used_by or '—'}\n"
        f"Инструкция:\n{c.instruction or '—'}",
        reply_markup=_back_kb(), link_preview_options=_NOPREV,
    )


@router.callback_query(F.data == "ac_setstatus")
async def cb_setstatus(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.setstatus)
    await call.message.edit_text(
        "✏️ Пришлите: <code>код | статус</code>\n"
        "статус: <code>free</code>/<code>свободен</code> или "
        "<code>used</code>/<code>использован</code>.",
        reply_markup=_back_kb(),
    )
    await call.answer()


@router.message(AC.setstatus)
async def msg_setstatus(message: Message, db: Database, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 2:
        await message.answer("Формат: код | статус")
        return
    code, raw = parts[0], parts[1].lower()
    status = "free" if raw in ("free", "свободен") else "used" if raw in ("used", "использован") else None
    if status is None:
        await message.answer("Статус: free/свободен или used/использован.")
        return
    async with db.session() as s:
        c = await act.find_code(s, code)
        if not c:
            await state.clear()
            await message.answer("🚫 Код не найден.", reply_markup=_back_kb())
            return
        c.status = status
        if status == "free":
            c.used_by = None
            c.used_at = None
        await s.commit()
    await state.clear()
    await message.answer(f"✅ Статус кода <code>{code}</code>: <b>{status}</b>", reply_markup=_back_kb())


# ── Roblox ────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "ac_roblox")
async def cb_roblox(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.roblox)
    await call.message.edit_text("🎮 Пришлите ник Roblox:", reply_markup=_back_kb())
    await call.answer()


@router.message(AC.roblox)
async def msg_roblox(message: Message, state: FSMContext) -> None:
    nick = (message.text or "").strip()
    await state.clear()
    if not nick:
        await message.answer("Пришлите ник.", reply_markup=_back_kb())
        return
    status = await message.answer("🔎 Проверяю аккаунт…")
    try:
        report, _uid, _actual = await asyncio.to_thread(roblox.build_gamepass_report, nick)
    except Exception as e:  # noqa: BLE001
        report = f"⚠️ Ошибка Roblox: {e}"
    if len(report) > 4000:
        report = report[:3950] + "\n\n… (обрезано)"
    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass
    await message.answer(report, reply_markup=_back_kb(), link_preview_options=_NOPREV)


# ── Блокировки / пользователи ─────────────────────────────────────────────────
@router.callback_query(F.data == "ac_block")
async def cb_block(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.block)
    await call.message.edit_text(
        "🚫 Пришлите: <code>user_id | причина</code>", reply_markup=_back_kb()
    )
    await call.answer()


@router.message(AC.block)
async def msg_block(message: Message, db: Database, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|", 1)]
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: user_id | причина")
        return
    uid, reason = int(parts[0]), parts[1]
    async with db.session() as s:
        u = await get_user(s, uid)
        if u is None:
            u = User(id=uid)
            s.add(u)
        u.is_blocked = True
        u.block_reason = reason
        await s.commit()
    await state.clear()
    try:
        await message.bot.send_message(uid, f"⛔ Доступ к боту заблокирован.\nПричина: {reason}")
    except Exception:  # noqa: BLE001
        pass
    await message.answer(f"🔒 Пользователь {uid} заблокирован.", reply_markup=_back_kb())


@router.callback_query(F.data == "ac_unblock")
async def cb_unblock(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.unblock)
    await call.message.edit_text("✅ Пришлите user_id для разблокировки:", reply_markup=_back_kb())
    await call.answer()


@router.message(AC.unblock)
async def msg_unblock(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Пришлите числовой user_id.")
        return
    uid = int(raw)
    async with db.session() as s:
        u = await get_user(s, uid)
        if u:
            u.is_blocked = False
            u.block_reason = None
            await s.commit()
    await state.clear()
    try:
        await message.bot.send_message(uid, "✅ Доступ к боту восстановлен.")
    except Exception:  # noqa: BLE001
        pass
    await message.answer(f"🔓 Пользователь {uid} разблокирован.", reply_markup=_back_kb())


@router.callback_query(F.data == "ac_blocked")
async def cb_blocked(call: CallbackQuery, db: Database) -> None:
    async with db.session() as s:
        rows = list(await s.execute(
            select(User.id, User.block_reason).where(User.is_blocked.is_(True)).limit(50)
        ))
    if not rows:
        text = "✅ Заблокированных нет."
    else:
        text = "⛔ <b>Заблокированные</b>\n\n" + "\n".join(
            f"🆔 {uid} — {reason or '—'}" for uid, reason in rows
        )
    await call.message.edit_text(text, reply_markup=_back_kb())
    await call.answer()


@router.callback_query(F.data == "ac_usercount")
async def cb_usercount(call: CallbackQuery, db: Database) -> None:
    async with db.session() as s:
        total = int(await s.scalar(select(func.count()).select_from(User)) or 0)
        blocked = int(await s.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        ) or 0)
    await call.message.edit_text(
        f"👥 Пользователей: <b>{total}</b>\nЗаблокировано: <b>{blocked}</b>",
        reply_markup=_back_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "ac_addusers")
async def cb_addusers(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.addusers)
    await call.message.edit_text(
        "➕ Пришлите user_id через запятую: <code>111,222,333</code>", reply_markup=_back_kb()
    )
    await call.answer()


@router.message(AC.addusers)
async def msg_addusers(message: Message, db: Database, state: FSMContext) -> None:
    ids = [x.strip() for x in (message.text or "").replace(";", ",").split(",")]
    added = 0
    async with db.session() as s:
        for x in ids:
            if not x.isdigit():
                continue
            uid = int(x)
            if await get_user(s, uid) is None:
                s.add(User(id=uid))
                added += 1
        await s.commit()
    await state.clear()
    await message.answer(f"✅ Добавлено пользователей: {added}", reply_markup=_back_kb())


# ── Сообщения / рассылка ──────────────────────────────────────────────────────
@router.callback_query(F.data == "ac_msg")
async def cb_msg(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.msg)
    await call.message.edit_text(
        "✉️ Пришлите: <code>user_id | текст</code>", reply_markup=_back_kb()
    )
    await call.answer()


@router.message(AC.msg)
async def msg_msg(message: Message, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|", 1)]
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer("Формат: user_id | текст")
        return
    uid, text = int(parts[0]), parts[1]
    await state.clear()
    try:
        await message.bot.send_message(uid, f"📢 Сообщение от администратора:\n\n{text}")
        await message.answer("✅ Отправлено.", reply_markup=_back_kb())
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не отправлено: {e}", reply_markup=_back_kb())


@router.callback_query(F.data == "ac_broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AC.broadcast)
    await call.message.edit_text(
        "📣 Пришлите: <code>Текст | Кнопка | URL</code>\n"
        "(кнопка и URL необязательны — можно только текст)",
        reply_markup=_back_kb(),
    )
    await call.answer()


@router.message(AC.broadcast)
async def msg_broadcast(message: Message, db: Database, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.text or "").split("|")]
    text = parts[0]
    if not text:
        await message.answer("Пустой текст.")
        return
    markup = None
    if len(parts) >= 3 and parts[1] and parts[2]:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text=parts[1], url=parts[2]))
        markup = b.as_markup()
    async with db.session() as s:
        ids = list(await s.scalars(select(User.id).where(User.is_blocked.is_(False))))
    await state.clear()
    await message.answer(f"📣 Рассылка по {len(ids)} пользователям…")
    sent = failed = 0
    for uid in ids:
        try:
            await message.bot.send_message(uid, text, reply_markup=markup, link_preview_options=_NOPREV)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)  # щадим лимиты Telegram
    await message.answer(
        f"✅ Готово. Отправлено: {sent}, ошибок: {failed}", reply_markup=_back_kb()
    )

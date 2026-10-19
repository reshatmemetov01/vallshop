"""Движок настройки канала (обязательная подписка) — головной и франшиза.

Регистрируется через :func:`register` на роутер магазина. Позволяет задать
ссылку-кнопку, цель проверки (chat) и включить обязательную подписку.

Публичный канал: достаточно прислать @username (ставит и ссылку, и цель проверки).
Приватный канал: переслать пост из канала (узнаём его id) + задать ссылку-кнопку
(invite-ссылку). Для обязательной подписки бот должен быть АДМИНОМ канала.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import channel as channel_service

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def _bt(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _parse_link(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Вернуть (url_для_кнопки, chat_для_проверки|None). None,None — не распознали."""
    raw = (raw or "").strip()
    low = raw.lower()
    # Приватная invite-ссылка — только ссылка-кнопка (id из неё не узнать).
    if low.startswith(("https://t.me/+", "https://t.me/joinchat", "t.me/+", "t.me/joinchat")):
        return (raw if raw.startswith("http") else "https://" + raw), None
    uname = None
    if raw.startswith("@"):
        uname = raw[1:]
    elif low.startswith(("https://t.me/", "t.me/")):
        tail = raw.split("t.me/", 1)[1].strip("/")
        if tail and not tail.startswith("+") and "/" not in tail:
            uname = tail
    if uname and _USERNAME_RE.match(uname):
        return "https://t.me/" + uname, "@" + uname
    if low.startswith(("http://", "https://")):
        return raw, None
    return None, None


def _forward_chat(message: Message):
    """Канал из пересланного сообщения (aiogram: forward_origin / forward_from_chat)."""
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin is not None else None
    if chat is None:
        chat = getattr(message, "forward_from_chat", None)
    if chat is not None and getattr(chat, "type", None) == "channel":
        return chat
    return None


async def _show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await call.message.answer(text, reply_markup=markup)


def register(router: Router, *, prefix: str, home_cb: str, states: type[StatesGroup],
             post_prefix: Optional[str] = None) -> dict:
    P = prefix
    S = states

    async def _home_text(merchant_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        cfg = channel_service.config(merchant_id)
        url = cfg.get("url") or "—"
        chat = cfg.get("chat") or "—"
        req = cfg.get("required")
        can_check = "да" if cfg.get("chat") else "нет (привяжите канал/@username)"
        warn = ""
        if cfg.get("chat") and not cfg.get("url"):
            warn = ("\n⚠️ Нет ссылки-кнопки — в сообщении о подписке не будет кнопки "
                    "на канал. Задайте её кнопкой «🔗 Ссылка».")
        text = (
            "📢 <b>Канал</b>\n\n"
            f"Ссылка-кнопка: {url}\n"
            f"Цель проверки: <code>{chat}</code>\n"
            f"Проверка подписки возможна: {can_check}\n"
            f"Обязательная подписка: <b>{'ВКЛ 🟢' if req else 'ВЫКЛ ⚪'}</b>"
            f"{warn}\n\n"
            "Публичный канал — пришлите @username. Приватный — перешлите пост из "
            "канала и задайте ссылку-кнопку. Для обязательной подписки бот должен "
            "быть <b>администратором</b> канала."
        )
        b = InlineKeyboardBuilder()
        b.row(_bt("🔗 Ссылка / @username", f"{P}:link"))
        b.row(_bt("📎 Привязать приватный (переслать пост)", f"{P}:fwd"))
        b.row(_bt(("🔔 Выключить обязательную" if req else "🔕 Включить обязательную"),
                  f"{P}:toggle"))
        if post_prefix:
            b.row(_bt("📝 Создать пост", f"{post_prefix}:new"),
                  _bt("📊 Клики постов", f"{post_prefix}:ds"))
        b.row(_bt("🗑 Убрать канал", f"{P}:clear"))
        b.row(_bt("⬅️ Назад", home_cb))
        return text, b.as_markup()

    @router.callback_query(F.data == f"{P}:home")
    async def cb_home(call: CallbackQuery, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await state.clear()
        text, markup = await _home_text(merchant_id)
        await _show(call, text, markup)
        await call.answer()

    @router.callback_query(F.data == f"{P}:link")
    async def cb_link(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.wait_link)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:home"))
        await _show(
            call,
            "🔗 Пришлите <b>@username</b> публичного канала (тогда и проверка заработает), "
            "или ссылку-кнопку: <code>https://t.me/...</code> / invite-ссылку "
            "<code>https://t.me/+...</code>",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.wait_link)
    async def msg_link(message: Message, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
        url, chat = _parse_link(message.text or "")
        if not url:
            await message.answer("Не распознал. Пришлите @username или ссылку t.me.")
            return
        await channel_service.set_url(db, merchant_id, url)
        note = ""
        if chat:
            await channel_service.set_chat(db, merchant_id, chat)
            note = "\nЦель проверки: " + chat
        else:
            note = ("\n⚠️ Для <b>обязательной</b> подписки на приватный канал ещё "
                    "перешлите пост из канала (кнопка «Привязать приватный»).")
        await state.clear()
        b = InlineKeyboardBuilder().row(_bt("⬅️ К каналу", f"{P}:home"))
        await message.answer(f"✅ Ссылка сохранена: {url}{note}", reply_markup=b.as_markup())

    @router.callback_query(F.data == f"{P}:fwd")
    async def cb_fwd(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.wait_forward)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:home"))
        await _show(
            call,
            "📎 Перешлите сюда любой пост из вашего канала.\n"
            "Бот должен быть администратором канала — тогда узнаем его id для проверки.",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.wait_forward)
    async def msg_fwd(message: Message, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
        chat = _forward_chat(message)
        if chat is None:
            await message.answer("Это не пересланный пост из канала. "
                                 "Перешлите пост именно из канала.")
            return
        await channel_service.set_chat(db, merchant_id, str(chat.id))
        title = getattr(chat, "title", "") or str(chat.id)
        if getattr(chat, "username", None):
            # Публичный канал — ссылка-кнопка строится из username автоматически.
            await channel_service.set_url(db, merchant_id, f"https://t.me/{chat.username}")
            await state.clear()
            b = InlineKeyboardBuilder().row(_bt("⬅️ К каналу", f"{P}:home"))
            await message.answer(
                f"✅ Канал «{title}» привязан (id <code>{chat.id}</code>).\n"
                "Убедитесь, что бот — администратор канала.",
                reply_markup=b.as_markup(),
            )
            return
        # Приватный канал — сразу просим invite-ссылку для кнопки «Перейти в канал».
        await state.set_state(S.wait_link)
        await message.answer(
            f"✅ Канал «{title}» привязан (id <code>{chat.id}</code>).\n\n"
            "Канал приватный — пришлите <b>invite-ссылку</b> "
            "(<code>https://t.me/+...</code>), чтобы у пользователей была кнопка "
            "«Перейти в канал».",
        )

    @router.callback_query(F.data == f"{P}:toggle")
    async def cb_toggle(call: CallbackQuery, db: Database, state: FSMContext,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
        cfg = channel_service.config(merchant_id)
        new_req = not cfg.get("required")
        if new_req and not cfg.get("chat"):
            await call.answer("Сначала привяжите канал (@username или пересылкой поста).",
                              show_alert=True)
            return
        if new_req and not cfg.get("url"):
            await call.answer("Сначала задайте ссылку-кнопку на канал (кнопка «🔗 Ссылка»), "
                              "иначе пользователям негде подписаться.", show_alert=True)
            return
        await channel_service.set_required(db, merchant_id, new_req)
        text, markup = await _home_text(merchant_id)
        await _show(call, text, markup)
        await call.answer("Включено ✅" if new_req else "Выключено")

    @router.callback_query(F.data == f"{P}:clear")
    async def cb_clear(call: CallbackQuery, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await channel_service.clear(db, merchant_id)
        text, markup = await _home_text(merchant_id)
        await _show(call, text, markup)
        await call.answer("Канал убран")

    return {
        "home": cb_home, "link": cb_link, "msg_link": msg_link, "fwd": cb_fwd,
        "msg_fwd": msg_fwd, "toggle": cb_toggle, "clear": cb_clear,
    }

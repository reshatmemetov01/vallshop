"""Движок рассылки: композер + конструктор инлайн-кнопок + предпросмотр + отправка.

Один и тот же флоу используется головным админом (в разделе «Статистика») и
владельцем франшизы (в кабинете). Отличия — только набор разделов бота для
кнопок, точка возврата «Назад» и область получателей (по ``merchant_id``).

Регистрация через :func:`register`: вешает хендлеры на переданный роутер (с его
фильтром доступа) под своим префиксом колбэков и своей FSM-группой, чтобы флоу
головного и франшизы не пересекались.

Черновик живёт в FSM-данных:
    d_text  — html-текст (с форматированием/премиум-эмодзи) или None
    d_photo — file_id фото или None
    d_btns  — список кнопок поста: {label, kind: cb|url, target, style, icon}
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import broadcast as broadcast_service
from ..services import stats as stats_service
from .. import keyboards as kb

MAX_BUTTONS = 5

# Разделы бота, на которые можно поставить кнопку (ключ-колбэк, подпись, эмодзи).
ALL_SECTIONS = [
    ("catalog",  "Каталог",          "catalog"),
    ("profile",  "Профиль",          "profile"),
    ("topup",    "Пополнить баланс", "topup"),
    ("faq",      "FAQ / Правила",    "faq"),
    ("reviews",  "Отзывы",           "reviews"),
    ("earn",     "Заработать",       "earn"),
    ("activate", "Активировать код", "activate"),
    ("menu",     "Главное меню",     "back"),
]
HEAD_SECTIONS = ALL_SECTIONS
# У франшизы нет «Активировать код» (раздел только головного).
FRANCHISE_SECTIONS = [s for s in ALL_SECTIONS if s[0] != "activate"]

# Цвета кнопок (style кастомного Bot API): значение → подпись.
COLORS = [
    ("-", "⚪ Обычный"),
    ("primary", "🔵 Синий"),
    ("success", "🟢 Зелёный"),
    ("danger", "🔴 Красный"),
]
_COLOR_DOT = {None: "⚪", "primary": "🔵", "success": "🟢", "danger": "🔴"}


def _bt(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# Telegram-юзернейм: 5–32 символа [A-Za-z0-9_] (допускаем от 3 для каналов).
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def _valid_post_url(raw: str) -> Optional[str]:
    """Нормализовать и ПРОВЕРИТЬ ссылку для кнопки. Невалидное → None.

    Принимаем: http(s)://…, tg://…, t.me/…, @username, username.
    Отвергаем всё с пробелами и произвольный текст (иначе Telegram отклонит
    всю клавиатуру поста).
    """
    raw = (raw or "").strip()
    if not raw or any(c.isspace() for c in raw):
        return None
    low = raw.lower()
    if low.startswith(("http://", "https://", "tg://")):
        return raw
    if low.startswith(("t.me/", "telegram.me/", "www.t.me/")):
        return "https://" + raw
    uname = raw[1:] if raw.startswith("@") else raw
    if _USERNAME_RE.match(uname):
        return "https://t.me/" + uname
    return None


def _emoji_from_message(message: Message) -> Optional[str]:
    """custom_emoji_id из сообщения, '__reset__' для '0'/'-', иначе None."""
    raw = (message.text or "").strip()
    if raw in ("0", "-"):
        return "__reset__"
    for ent in (message.entities or []):
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            return ent.custom_emoji_id
    return None


async def _show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — сообщение-фото / без изменений
        await call.message.answer(text, reply_markup=markup)


def register(
    router: Router,
    *,
    prefix: str,
    home_cb: str,
    title: str,
    sections: list,
    states: type[StatesGroup],
) -> None:
    """Зарегистрировать хендлеры рассылки на ``router`` под ``prefix``."""

    P = prefix
    S = states

    # ── Вспомогательное: экраны ─────────────────────────────────────────────
    def _draft(data: dict) -> tuple[Optional[str], Optional[str], List[dict]]:
        return data.get("d_text"), data.get("d_photo"), list(data.get("d_btns") or [])

    def _editor_text(text: Optional[str], photo: Optional[str], btns: List[dict]) -> str:
        # Сам текст не вставляем (может быть длинным/с разметкой) — его точный вид
        # покажет «👁 Предпросмотр». Здесь только состав черновика.
        txt_line = f"есть ({len(text)} симв.)" if text else "нет"
        return (
            f"✍️ <b>Рассылка — черновик</b>\n\n"
            f"📝 Текст: <b>{txt_line}</b>\n"
            f"📷 Фото: <b>{'есть' if photo else 'нет'}</b>\n"
            f"🔘 Кнопок: <b>{len(btns)}/{MAX_BUTTONS}</b>\n\n"
            "Нажмите «👁 Предпросмотр», чтобы увидеть пост как клиенты."
        )

    def _editor_kb(btns: List[dict]) -> InlineKeyboardMarkup:
        b = InlineKeyboardBuilder()
        for i, btn in enumerate(btns):
            dot = _COLOR_DOT.get(btn.get("style"))
            b.row(_bt(f"{dot} {i + 1}. {btn['label']}", f"{P}:be:{i}"))
        if len(btns) < MAX_BUTTONS:
            b.row(_bt("➕ Добавить кнопку", f"{P}:add"))
        b.row(_bt("👁 Предпросмотр", f"{P}:prev"))
        b.row(_bt("✏️ Изменить текст/фото", f"{P}:new"))
        b.row(_bt("📤 Отправить", f"{P}:send"))
        b.row(_bt("⬅️ Отмена", f"{P}:home"))
        return b.as_markup()

    def _btn_card(i: int, btn: dict) -> tuple[str, InlineKeyboardMarkup]:
        kind = "раздел бота" if btn.get("kind") == "cb" else "ссылка"
        dot = _COLOR_DOT.get(btn.get("style"))
        emo = "есть" if btn.get("icon") else "нет"
        text = (
            f"🔘 <b>Кнопка {i + 1}</b>\n\n"
            f"Текст: <b>{btn['label']}</b>\n"
            f"Тип: {kind} (<code>{btn['target']}</code>)\n"
            f"Цвет: {dot}\n"
            f"Премиум-эмодзи: {emo}"
        )
        b = InlineKeyboardBuilder()
        b.row(_bt("✏️ Текст", f"{P}:bl:{i}"), _bt("🎨 Цвет", f"{P}:bc:{i}"))
        b.row(_bt("😀 Эмодзи", f"{P}:bm:{i}"), _bt("🗑 Удалить", f"{P}:bd:{i}"))
        b.row(_bt("✅ Готово", f"{P}:ed"))
        return text, b.as_markup()

    async def _goto_editor(call: CallbackQuery, state: FSMContext) -> None:
        text, photo, btns = _draft(await state.get_data())
        await _show(call, _editor_text(text, photo, btns), _editor_kb(btns))

    # ── Главная рассылки (внутри раздела «Статистика» / кабинета) ────────────
    @router.callback_query(F.data == f"{P}:home")
    async def cb_home(call: CallbackQuery, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await state.clear()
        count = len(await broadcast_service.recipients(db, merchant_id))
        async with db.session() as s:
            act = await stats_service.activity_stats(s, merchant_id)
        b = InlineKeyboardBuilder()
        b.row(_bt("✍️ Создать рассылку", f"{P}:new"))
        b.row(_bt("⬅️ Назад", home_cb))
        await _show(
            call,
            f"📣 <b>{title}</b>\n\n"
            f"Получатели: <b>{count}</b>\n"
            f"🟢 Активные: <b>{act['active']}</b> · "
            f"🔴 Неактивные: <b>{act['inactive']}</b>\n\n"
            "Активные снова становятся активными, как только пишут боту.",
            b.as_markup(),
        )
        await call.answer()

    @router.callback_query(F.data == f"{P}:new")
    async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.compose)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Отмена", f"{P}:home"))
        await _show(
            call,
            "✍️ Пришлите сообщение рассылки.\n\n"
            "Можно текст (с форматированием и премиум-эмодзи) или фото с подписью. "
            "Кнопки под постом добавите на следующем шаге.",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.compose)
    async def msg_compose(message: Message, state: FSMContext) -> None:
        photo = message.photo[-1].file_id if message.photo else None
        text = message.html_text if (message.text or message.caption) else None
        if not photo and not text:
            await message.answer("Пришлите текст или фото. Попробуйте ещё раз.")
            return
        await state.update_data(d_text=text, d_photo=photo)
        await state.set_state(None)
        _, _, btns = _draft(await state.get_data())
        await message.answer(_editor_text(text, photo, btns), reply_markup=_editor_kb(btns))

    @router.callback_query(F.data == f"{P}:ed")
    async def cb_editor(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(None)
        await _goto_editor(call, state)
        await call.answer()

    # ── Добавление кнопки: выбор типа ────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:add")
    async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
        _, _, btns = _draft(await state.get_data())
        if len(btns) >= MAX_BUTTONS:
            await call.answer("Достигнут лимит кнопок", show_alert=True)
            return
        b = InlineKeyboardBuilder()
        b.row(_bt("🔗 Раздел бота", f"{P}:ksec"))
        b.row(_bt("🌐 Своя ссылка", f"{P}:kurl"))
        b.row(_bt("⬅️ Назад", f"{P}:ed"))
        await _show(call, "Что добавить на кнопку?", b.as_markup())
        await call.answer()

    @router.callback_query(F.data == f"{P}:ksec")
    async def cb_ksec(call: CallbackQuery, state: FSMContext) -> None:
        b = InlineKeyboardBuilder()
        for key, label, _icon in sections:
            b.row(_bt(label, f"{P}:sp:{key}"))
        b.row(_bt("⬅️ Назад", f"{P}:add"))
        await _show(call, "Выберите раздел бота:", b.as_markup())
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:sp:"))
    async def cb_section_pick(call: CallbackQuery, state: FSMContext) -> None:
        key = call.data.split(":")[-1]
        meta = next((s for s in sections if s[0] == key), None)
        if meta is None:
            await call.answer("Неизвестный раздел", show_alert=True)
            return
        _, _, btns = _draft(await state.get_data())
        if len(btns) >= MAX_BUTTONS:
            await call.answer("Достигнут лимит кнопок", show_alert=True)
            return
        _k, label, icon_key = meta
        btns.append({
            "label": label, "kind": "cb", "target": key,
            "style": "primary", "icon": kb.EMOJI.get(icon_key),
        })
        await state.update_data(d_btns=btns)
        text, markup = _btn_card(len(btns) - 1, btns[-1])
        await _show(call, text, markup)
        await call.answer("Кнопка добавлена")

    @router.callback_query(F.data == f"{P}:kurl")
    async def cb_kurl(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.url)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:add"))
        await _show(
            call,
            "🌐 Пришлите ссылку для кнопки:\n"
            "• <code>https://…</code>\n• <code>t.me/username</code>\n• <code>@username</code>",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.url)
    async def msg_url(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        url = _valid_post_url(raw)
        if not url:
            await message.answer("Не похоже на ссылку. Пришлите URL, t.me/… или @username.")
            return
        _, _, btns = _draft(await state.get_data())
        if len(btns) >= MAX_BUTTONS:
            await state.set_state(None)
            await message.answer("Достигнут лимит кнопок.")
            return
        label = raw.lstrip("@").replace("https://", "").replace("http://", "")[:32] or "Открыть"
        btns.append({"label": label, "kind": "url", "target": url, "style": "primary", "icon": None})
        await state.update_data(d_btns=btns)
        await state.set_state(None)
        text, markup = _btn_card(len(btns) - 1, btns[-1])
        await message.answer(text, reply_markup=markup)

    # ── Правка одной кнопки ──────────────────────────────────────────────────
    def _idx(data: str) -> int:
        return int(data.split(":")[-1])

    async def _card_by_idx(call: CallbackQuery, state: FSMContext, i: int) -> None:
        _, _, btns = _draft(await state.get_data())
        if not (0 <= i < len(btns)):
            await _goto_editor(call, state)
            return
        text, markup = _btn_card(i, btns[i])
        await _show(call, text, markup)

    @router.callback_query(F.data.startswith(f"{P}:be:"))
    async def cb_btn_edit(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(None)
        await _card_by_idx(call, state, _idx(call.data))
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:bl:"))
    async def cb_btn_label(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(edit_idx=_idx(call.data))
        await state.set_state(S.label)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:be:{_idx(call.data)}"))
        await _show(call, "✏️ Пришлите новый текст кнопки (до 48 символов).", b.as_markup())
        await call.answer()

    @router.message(S.label)
    async def msg_btn_label(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        i = int(data.get("edit_idx", -1))
        btns = list(data.get("d_btns") or [])
        label = (message.text or "").strip()[:48]
        if not label:
            await message.answer("Текст пустой. Пришлите текст кнопки.")
            return
        if 0 <= i < len(btns):
            btns[i]["label"] = label
            await state.update_data(d_btns=btns)
        await state.set_state(None)
        text, markup = _btn_card(i, btns[i])
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith(f"{P}:bc:"))
    async def cb_btn_color(call: CallbackQuery, state: FSMContext) -> None:
        i = _idx(call.data)
        b = InlineKeyboardBuilder()
        for val, label in COLORS:
            b.row(_bt(label, f"{P}:bs:{i}:{val}"))
        b.row(_bt("⬅️ Назад", f"{P}:be:{i}"))
        await _show(call, "🎨 Выберите цвет кнопки:", b.as_markup())
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:bs:"))
    async def cb_btn_setcolor(call: CallbackQuery, state: FSMContext) -> None:
        parts = call.data.split(":")  # P:bs:<idx>:<style>
        i, style = int(parts[2]), parts[3]
        _, _, btns = _draft(await state.get_data())
        if 0 <= i < len(btns):
            btns[i]["style"] = None if style == "-" else style
            await state.update_data(d_btns=btns)
        await _card_by_idx(call, state, i)
        await call.answer("Цвет изменён")

    @router.callback_query(F.data.startswith(f"{P}:bm:"))
    async def cb_btn_emoji(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(edit_idx=_idx(call.data))
        await state.set_state(S.emoji)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:be:{_idx(call.data)}"))
        await _show(
            call,
            "😀 Пришлите премиум-эмодзи (одним сообщением) для кнопки.\n"
            "Чтобы убрать эмодзи — пришлите <code>0</code>.",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.emoji)
    async def msg_btn_emoji(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        i = int(data.get("edit_idx", -1))
        btns = list(data.get("d_btns") or [])
        emo = _emoji_from_message(message)
        if emo is None:
            await message.answer("Это не премиум-эмодзи. Пришлите премиум-эмодзи или 0.")
            return
        if 0 <= i < len(btns):
            btns[i]["icon"] = None if emo == "__reset__" else emo
            await state.update_data(d_btns=btns)
        await state.set_state(None)
        text, markup = _btn_card(i, btns[i])
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.startswith(f"{P}:bd:"))
    async def cb_btn_delete(call: CallbackQuery, state: FSMContext) -> None:
        i = _idx(call.data)
        _, _, btns = _draft(await state.get_data())
        if 0 <= i < len(btns):
            btns.pop(i)
            await state.update_data(d_btns=btns)
        await _goto_editor(call, state)
        await call.answer("Кнопка удалена")

    # ── Предпросмотр ─────────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:prev")
    async def cb_preview(call: CallbackQuery, state: FSMContext) -> None:
        text, photo, btns = _draft(await state.get_data())
        if not text and not photo:
            await call.answer("Сначала добавьте текст или фото", show_alert=True)
            return
        markup = kb.broadcast_post_kb(btns)
        try:
            if photo:
                await call.message.answer_photo(photo, caption=text or None, reply_markup=markup)
            else:
                await call.message.answer(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 — напр. некорректная ссылка в кнопке
            await call.answer("Не удалось показать превью (проверьте кнопки/ссылки).",
                              show_alert=True)
            return
        b = InlineKeyboardBuilder().row(_bt("⬅️ К редактору", f"{P}:ed"))
        await call.message.answer("👆 Так увидят клиенты.", reply_markup=b.as_markup())
        await call.answer()

    # ── Подтверждение и отправка ─────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:send")
    async def cb_send_confirm(call: CallbackQuery, db: Database, state: FSMContext,
                              merchant_id: int = HEAD_MERCHANT_ID) -> None:
        text, photo, btns = _draft(await state.get_data())
        if not text and not photo:
            await call.answer("Нечего отправлять", show_alert=True)
            return
        count = len(await broadcast_service.recipients(db, merchant_id))
        b = InlineKeyboardBuilder()
        b.row(_bt("✅ Да, отправить", f"{P}:go"))
        b.row(_bt("⬅️ Назад", f"{P}:ed"))
        await _show(
            call,
            f"📤 Отправить рассылку <b>{count}</b> клиентам?\n"
            f"Кнопок под постом: <b>{len(btns)}</b>.",
            b.as_markup(),
        )
        await call.answer()

    @router.callback_query(F.data == f"{P}:go")
    async def cb_go(call: CallbackQuery, db: Database, state: FSMContext,
                    merchant_id: int = HEAD_MERCHANT_ID) -> None:
        text, photo, btns = _draft(await state.get_data())
        await state.clear()
        if not text and not photo:
            await call.answer("Нечего отправлять", show_alert=True)
            return
        user_ids = await broadcast_service.recipients(db, merchant_id)
        markup = kb.broadcast_post_kb(btns)
        bot = call.message.bot
        owner_id = call.from_user.id

        async def _job():
            sent, failed, ok_ids, blocked_ids = await broadcast_service.run(
                bot, user_ids, text=text, photo=photo, reply_markup=markup
            )
            await broadcast_service.apply_delivery(db, merchant_id, ok_ids, blocked_ids)
            async with db.session() as s:
                act = await stats_service.activity_stats(s, merchant_id)
            try:
                await bot.send_message(
                    owner_id,
                    f"📣 Рассылка завершена.\n"
                    f"Отправлено: <b>{sent}</b> · не доставлено: <b>{failed}</b>.\n"
                    f"🟢 Активные: <b>{act['active']}</b> · "
                    f"🔴 Неактивные: <b>{act['inactive']}</b>.",
                )
            except Exception:  # noqa: BLE001
                pass

        asyncio.create_task(_job())
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", home_cb))
        await _show(
            call,
            f"📣 Рассылка запущена для <b>{len(user_ids)}</b> клиентов.\n"
            "По завершении пришлю отчёт и обновлю активных/неактивных.",
            b.as_markup(),
        )
        await call.answer("Запущено ✅")

    # Возвращаем хендлеры (для тестов); при обычной регистрации не используется.
    return {
        "home": cb_home, "new": cb_new, "compose": msg_compose, "editor": cb_editor,
        "add": cb_add, "ksec": cb_ksec, "section_pick": cb_section_pick,
        "kurl": cb_kurl, "url": msg_url, "btn_edit": cb_btn_edit,
        "btn_label": cb_btn_label, "label": msg_btn_label, "btn_color": cb_btn_color,
        "setcolor": cb_btn_setcolor, "btn_emoji": cb_btn_emoji, "emoji": msg_btn_emoji,
        "btn_delete": cb_btn_delete, "preview": cb_preview,
        "send_confirm": cb_send_confirm, "go": cb_go,
    }

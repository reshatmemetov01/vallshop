"""Создание и публикация поста в канал магазина (из раздела «Канал»).

Пост как в рассылке: текст с форматированием/премиум-эмодзи, фото, инлайн-кнопки
(разделы бота или ссылки), цвет кнопок. Кнопки-разделы считают клики (callback
cpk:…) и открывают бота по deep-link. Пост хранится как Broadcast(status='channel'),
клики — в broadcast_clicks (переиспользуем инфраструктуру рассылки).
"""

from __future__ import annotations

from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import broadcasts as broadcasts_service
from ..services import channel as channel_service
from . import broadcast_flow as bf
from .. import keyboards as kb
from .. import texts

MAX_BUTTONS = bf.MAX_BUTTONS


def _bt(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await call.message.answer(text, reply_markup=markup)


def register(router: Router, *, prefix: str, home_cb: str, sections: list,
             states: type[StatesGroup]) -> dict:
    P, S = prefix, states

    def _draft(d: dict):
        return d.get("d_text"), d.get("d_photo"), list(d.get("d_btns") or [])

    def _editor_kb(btns):
        b = InlineKeyboardBuilder()
        for i, btn in enumerate(btns):
            dot = bf._COLOR_DOT.get(btn.get("style"))
            b.row(_bt(f"{dot} {i + 1}. {btn['label']}", f"{P}:be:{i}"))
        if len(btns) < MAX_BUTTONS:
            b.row(_bt("➕ Добавить кнопку", f"{P}:add"))
        b.row(_bt("👁 Предпросмотр", f"{P}:prev"))
        b.row(_bt("✏️ Изменить текст/фото", f"{P}:new"))
        b.row(_bt("📤 Опубликовать", f"{P}:send"))
        b.row(_bt("⬅️ Отмена", home_cb))
        return b.as_markup()

    def _editor_text(text, photo, btns):
        tl = f"есть ({len(text)} симв.)" if text else "нет"
        return (f"📝 <b>Пост в канал</b>\n\n📝 Текст: <b>{tl}</b>\n"
                f"📷 Фото: <b>{'есть' if photo else 'нет'}</b>\n"
                f"🔘 Кнопок: <b>{len(btns)}/{MAX_BUTTONS}</b>")

    def _btn_card(i, btn):
        kind = "раздел бота" if btn.get("kind") == "cb" else "ссылка"
        dot = bf._COLOR_DOT.get(btn.get("style"))
        emo = "есть" if btn.get("icon") else "нет"
        text = (f"🔘 <b>Кнопка {i + 1}</b>\n\nТекст: <b>{btn['label']}</b>\n"
                f"Тип: {kind} (<code>{btn['target']}</code>)\nЦвет: {dot}\n"
                f"Премиум-эмодзи: {emo}")
        b = InlineKeyboardBuilder()
        b.row(_bt("✏️ Текст", f"{P}:bl:{i}"), _bt("🎨 Цвет", f"{P}:bc:{i}"))
        b.row(_bt("😀 Эмодзи", f"{P}:bm:{i}"), _bt("🗑 Удалить", f"{P}:bd:{i}"))
        b.row(_bt("✅ Готово", f"{P}:ed"))
        return text, b.as_markup()

    async def _editor(call, state):
        t, p, btns = _draft(await state.get_data())
        await _show(call, _editor_text(t, p, btns), _editor_kb(btns))

    def _idx(data):
        return int(data.split(":")[-1])

    # ── Композер ─────────────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:new")
    async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(S.compose)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Отмена", home_cb))
        await _show(call, "📝 Пришлите текст поста (с форматированием и премиум-эмодзи) "
                          "или фото с подписью.", b.as_markup())
        await call.answer()

    @router.message(S.compose)
    async def msg_compose(message: Message, state: FSMContext) -> None:
        photo = message.photo[-1].file_id if message.photo else None
        text = message.html_text if (message.text or message.caption) else None
        if not photo and not text:
            await message.answer("Пришлите текст или фото.")
            return
        await state.update_data(d_text=text, d_photo=photo)
        await state.set_state(None)
        _, _, btns = _draft(await state.get_data())
        await message.answer(_editor_text(text, photo, btns), reply_markup=_editor_kb(btns))

    @router.callback_query(F.data == f"{P}:ed")
    async def cb_ed(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(None)
        await _editor(call, state)
        await call.answer()

    # ── Конструктор кнопок ───────────────────────────────────────────────────
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
    async def cb_sp(call: CallbackQuery, state: FSMContext) -> None:
        key = call.data.split(":")[-1]
        meta = next((s for s in sections if s[0] == key), None)
        _, _, btns = _draft(await state.get_data())
        if meta is None or len(btns) >= MAX_BUTTONS:
            await call.answer("Нельзя", show_alert=True)
            return
        btns.append({"label": meta[1], "kind": "cb", "target": key,
                     "style": "primary", "icon": kb.EMOJI.get(meta[2])})
        await state.update_data(d_btns=btns)
        t, m = _btn_card(len(btns) - 1, btns[-1])
        await _show(call, t, m)
        await call.answer("Кнопка добавлена")

    @router.callback_query(F.data == f"{P}:kurl")
    async def cb_kurl(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.url)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:add"))
        await _show(call, "🌐 Пришлите ссылку (https://…, t.me/… или @username).", b.as_markup())
        await call.answer()

    @router.message(S.url)
    async def msg_url(message: Message, state: FSMContext) -> None:
        url = bf._valid_post_url(message.text or "")
        if not url:
            await message.answer("Не похоже на ссылку. Пришлите URL, t.me/… или @username.")
            return
        _, _, btns = _draft(await state.get_data())
        if len(btns) >= MAX_BUTTONS:
            await state.set_state(None)
            await message.answer("Достигнут лимит кнопок.")
            return
        label = (message.text or "").strip().lstrip("@").replace("https://", "")[:32] or "Открыть"
        btns.append({"label": label, "kind": "url", "target": url, "style": "primary", "icon": None})
        await state.update_data(d_btns=btns)
        await state.set_state(None)
        t, m = _btn_card(len(btns) - 1, btns[-1])
        await message.answer(t, reply_markup=m)

    @router.callback_query(F.data.startswith(f"{P}:be:"))
    async def cb_be(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(None)
        i = _idx(call.data)
        _, _, btns = _draft(await state.get_data())
        if 0 <= i < len(btns):
            t, m = _btn_card(i, btns[i])
            await _show(call, t, m)
        else:
            await _editor(call, state)
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:bl:"))
    async def cb_bl(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(edit_idx=_idx(call.data))
        await state.set_state(S.label)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:be:{_idx(call.data)}"))
        await _show(call, "✏️ Пришлите новый текст кнопки (до 48 символов).", b.as_markup())
        await call.answer()

    @router.message(S.label)
    async def msg_bl(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        i = int(data.get("edit_idx", -1))
        btns = list(data.get("d_btns") or [])
        label = (message.text or "").strip()[:48]
        if not label:
            await message.answer("Текст пустой.")
            return
        if 0 <= i < len(btns):
            btns[i]["label"] = label
            await state.update_data(d_btns=btns)
        await state.set_state(None)
        t, m = _btn_card(i, btns[i])
        await message.answer(t, reply_markup=m)

    @router.callback_query(F.data.startswith(f"{P}:bc:"))
    async def cb_bc(call: CallbackQuery, state: FSMContext) -> None:
        i = _idx(call.data)
        b = InlineKeyboardBuilder()
        for val, label in bf.COLORS:
            b.row(_bt(label, f"{P}:bs:{i}:{val}"))
        b.row(_bt("⬅️ Назад", f"{P}:be:{i}"))
        await _show(call, "🎨 Выберите цвет кнопки:", b.as_markup())
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:bs:"))
    async def cb_bs(call: CallbackQuery, state: FSMContext) -> None:
        parts = call.data.split(":")
        i, style = int(parts[2]), parts[3]
        _, _, btns = _draft(await state.get_data())
        if 0 <= i < len(btns):
            btns[i]["style"] = None if style == "-" else style
            await state.update_data(d_btns=btns)
            t, m = _btn_card(i, btns[i])
            await _show(call, t, m)
        await call.answer("Цвет изменён")

    @router.callback_query(F.data.startswith(f"{P}:bm:"))
    async def cb_bm(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(edit_idx=_idx(call.data))
        await state.set_state(S.emoji)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:be:{_idx(call.data)}"))
        await _show(call, "😀 Пришлите премиум-эмодзи для кнопки. <code>0</code> — убрать.",
                    b.as_markup())
        await call.answer()

    @router.message(S.emoji)
    async def msg_bm(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        i = int(data.get("edit_idx", -1))
        btns = list(data.get("d_btns") or [])
        emo = bf._emoji_from_message(message)
        if emo is None:
            await message.answer("Это не премиум-эмодзи. Пришлите премиум-эмодзи или 0.")
            return
        if 0 <= i < len(btns):
            btns[i]["icon"] = None if emo == "__reset__" else emo
            await state.update_data(d_btns=btns)
        await state.set_state(None)
        t, m = _btn_card(i, btns[i])
        await message.answer(t, reply_markup=m)

    @router.callback_query(F.data.startswith(f"{P}:bd:"))
    async def cb_bd(call: CallbackQuery, state: FSMContext) -> None:
        i = _idx(call.data)
        _, _, btns = _draft(await state.get_data())
        if 0 <= i < len(btns):
            btns.pop(i)
            await state.update_data(d_btns=btns)
        await _editor(call, state)
        await call.answer("Удалена")

    # ── Предпросмотр и публикация ────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:prev")
    async def cb_prev(call: CallbackQuery, state: FSMContext) -> None:
        t, p, btns = _draft(await state.get_data())
        if not t and not p:
            await call.answer("Сначала добавьте текст или фото", show_alert=True)
            return
        markup = kb.broadcast_post_kb(btns)  # предпросмотр (без трекинга)
        try:
            if p:
                await call.message.answer_photo(p, caption=t or None, reply_markup=markup)
            else:
                await call.message.answer(t, reply_markup=markup)
        except Exception:  # noqa: BLE001
            await call.answer("Не удалось показать превью (проверьте кнопки/ссылки).",
                              show_alert=True)
            return
        b = InlineKeyboardBuilder().row(_bt("⬅️ К редактору", f"{P}:ed"))
        await call.message.answer("👆 Так увидят в канале.", reply_markup=b.as_markup())
        await call.answer()

    @router.callback_query(F.data == f"{P}:send")
    async def cb_send(call: CallbackQuery, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
        t, p, btns = _draft(await state.get_data())
        if not t and not p:
            await call.answer("Нечего публиковать", show_alert=True)
            return
        if not channel_service.config(merchant_id).get("chat"):
            b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", home_cb))
            await _show(call, "❌ Канал не привязан. Сначала привяжите канал в разделе «Канал».",
                        b.as_markup())
            await call.answer()
            return
        b = InlineKeyboardBuilder()
        b.row(_bt("✅ Опубликовать в канал", f"{P}:go"))
        b.row(_bt("⬅️ Отмена", f"{P}:ed"))
        await _show(call, f"📤 Опубликовать пост в канал?\nКнопок: <b>{len(btns)}</b>.",
                    b.as_markup())
        await call.answer()

    @router.callback_query(F.data == f"{P}:go")
    async def cb_go(call: CallbackQuery, db: Database, state: FSMContext,
                    merchant_id: int = HEAD_MERCHANT_ID) -> None:
        t, p, btns = _draft(await state.get_data())
        await state.clear()
        cfg = channel_service.config(merchant_id)
        chat = cfg.get("chat")
        back = InlineKeyboardBuilder().row(_bt("⬅️ Назад", home_cb)).as_markup()
        if (not t and not p) or not chat:
            await _show(call, "❌ Нечего публиковать или канал не привязан.", back)
            await call.answer()
            return
        async with db.session() as s:
            post = await broadcasts_service.create(
                s, merchant_id=merchant_id, created_by=call.from_user.id,
                text=t, photo=p, buttons=btns, status="channel",
            )
        markup = kb.channel_post_kb(btns, post.id)
        target = int(chat) if str(chat).lstrip("-").isdigit() else chat
        bot = call.message.bot
        try:
            if p:
                await bot.send_photo(target, p, caption=t or None, reply_markup=markup)
            else:
                await bot.send_message(target, t, reply_markup=markup)
        except Exception:  # noqa: BLE001
            await _show(call, "❌ Не удалось опубликовать. Проверьте, что бот — "
                              "администратор канала с правом публикации сообщений.", back)
            await call.answer()
            return
        await _show(call, "✅ Пост опубликован в канал.", back)
        await call.answer("Опубликовано ✅")

    # ── Статистика кликов ────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:ds")
    async def cb_stats(call: CallbackQuery, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await state.clear()
        async with db.session() as s:
            posts = await broadcasts_service.channel_posts(s, merchant_id, limit=10)
        b = InlineKeyboardBuilder()
        if not posts:
            body = "📊 <b>Посты в канал</b>\n\nПока не было."
        else:
            body = "📊 <b>Посты в канал</b>\n\nВыберите пост:"
            for post in posts:
                d = post.created_at.strftime("%d.%m %H:%M") if post.created_at else ""
                b.row(_bt(f"#{post.id} · {d}", f"{P}:sd:{post.id}"))
        b.row(_bt("⬅️ Назад", home_cb))
        await _show(call, body, b.as_markup())
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:sd:"))
    async def cb_stat_detail(call: CallbackQuery, db: Database,
                             merchant_id: int = HEAD_MERCHANT_ID) -> None:
        pid = int(call.data.split(":")[-1])
        async with db.session() as s:
            post = await broadcasts_service.get(s, pid, merchant_id)
            if post is None:
                await call.answer("Не найдено", show_alert=True)
                return
            stats = await broadcasts_service.button_stats(s, pid)
            btns = broadcasts_service.buttons_of(post)
        lines = [f"📊 <b>Пост #{pid}</b>\n", "<b>Клики по кнопкам</b>"]
        if not btns:
            lines.append("Кнопок не было.")
        else:
            any_url = False
            for i, btn in enumerate(btns):
                if btn.get("kind") == "url":
                    any_url = True
                    lines.append(f"🌐 {btn.get('label')}: ссылка (клики не считаются)")
                else:
                    st = stats.get(i, {"uniq": 0, "total": 0})
                    lines.append(f"🔘 {btn.get('label')}: уник. <b>{st['uniq']}</b> · "
                                 f"всего <b>{st['total']}</b>")
            if any_url:
                lines.append("\n<i>Клики по ссылкам Telegram не сообщает.</i>")
        b = InlineKeyboardBuilder().row(_bt("⬅️ К списку", f"{P}:ds"))
        await _show(call, "\n".join(lines), b.as_markup())
        await call.answer()

    return {
        "new": cb_new, "compose": msg_compose, "add": cb_add, "sp": cb_sp,
        "url": msg_url, "send": cb_send, "go": cb_go, "stats": cb_stats,
        "stat_detail": cb_stat_detail,
    }

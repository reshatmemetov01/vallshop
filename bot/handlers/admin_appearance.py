"""Админ-раздел «Оформление»: тексты, эмодзи, порядок и видимость кнопок меню."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fazercard import FazerCardClient

from ..config import BotConfig
from ..db import Database
from ..db.models import Product, Variant
from ..services import catalog as catalog_service
from ..services import menu as menu_service
from ..services import paymethods as paymethods_service

router = Router(name="admin_appearance")


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class APState(StatesGroup):
    set_label = State()
    set_emoji = State()
    set_caption = State()
    set_pay_label = State()
    set_pay_heading = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _cancel(back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(_btn("⬅️ Отмена", back)).as_markup()


# Цвета кнопок меню: без цвета → синий → зелёный → красный → ...
_MENU_COLOR_CYCLE = ["", "primary", "success", "danger"]
_MENU_COLOR_NAME = {None: "⚪ без цвета", "": "⚪ без цвета", "primary": "🔵 синий",
                    "success": "🟢 зелёный", "danger": "🔴 красный"}


# ── Главный экран «Оформление» ───────────────────────────────────────────────
@router.callback_query(F.data == "ap_home")
async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📝 Тексты экранов", "ap_texts"))
    for m, title in menu_service.MENU_TITLES.items():
        kb.row(_btn(f"📋 Кнопки: {title}", f"ap_menu:{m}"))
    kb.row(_btn("🛍 Кнопки каталога", "apc_home"))
    kb.row(_btn("💳 Способы оплаты", "pm_home"))
    kb.row(_btn("♻️ Перевести Цены", "ap_migprices"))
    kb.row(_btn("♻️ Сбросить к стандартному", "ap_reset"))
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(
        "🎨 <b>Оформление</b>\n\n"
        "Настройка клиентских кнопок: текст, премиум-эмодзи, порядок и "
        "видимость (вкл/выкл — например, скрыть на время обновления).\n\n"
        "Выберите меню:",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "ap_migprices")
async def cb_migprices_ask(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("♻️ Да, перевести", "ap_migprices_ok"))
    kb.row(_btn("⬅️ Отмена", "ap_home"))
    await call.message.edit_text(
        "♻️ <b>Перевести цены на умную цену</b>\n\n"
        "Пройду по товарам <b>поставщика</b> (FazerCard и т.п., где есть живой закуп), "
        "возьму текущий закуп и зафиксирую из текущей цены наценку. Дальше цена сама "
        "будет плавать от закупа (порог 1%), и при росте &gt; 1% придёт уведомление.\n\n"
        "❗️ Не трогаю: свой сток кодов, LioGames, VPN и всё, что на 📌 Фикс ₽.\n"
        "После перевода загляни в пару номиналов — проверь получившиеся наценки.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "ap_migprices_ok")
async def cb_migprices_ok(
    call: CallbackQuery, db: Database, config: BotConfig, fzr: FazerCardClient
) -> None:
    await call.answer("Перевожу…")
    from ..services import smartprice as smartprice_service
    converted, skipped = await smartprice_service.migrate_supplier_to_smart(
        db, fzr, config.default_markup_percent
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К оформлению", "ap_home"))
    await call.message.edit_text(
        f"✅ Готово.\nПереведено на умную цену: <b>{converted}</b>\n"
        f"Пропущено (фикс / уже умные / нет закупа): <b>{skipped}</b>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "ap_reset")
async def cb_reset_ask(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("♻️ Да, сбросить", "ap_reset_ok"))
    kb.row(_btn("⬅️ Отмена", "ap_home"))
    await call.message.edit_text(
        "♻️ Сбросить <b>всё оформление</b> к стандартному?\n"
        "Тексты кнопок, эмодзи, порядок и тексты экранов вернутся к значениям "
        "по умолчанию. Скрытые кнопки снова станут видимыми.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "ap_reset_ok")
async def cb_reset(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await menu_service.reset_all(db)
    await state.clear()
    await cb_home(call, state)
    await call.answer("Сброшено ✅")


@router.callback_query(F.data.startswith("ap_menu:"))
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    menu = call.data.split(":", 1)[1]
    await _show_menu(call, menu)
    await call.answer()


async def _show_menu(call: CallbackQuery, menu: str) -> None:
    kb = InlineKeyboardBuilder()
    for r in menu_service.menu_entries(menu, only_enabled=False):
        mark = "🟢" if r.enabled else "🔴"
        kb.row(_btn(f"{mark} {r.text}", f"ap_btn:{r.key}"))
    kb.row(_btn("⬅️ Назад", "ap_home"))
    title = menu_service.MENU_TITLES.get(menu, menu)
    await call.message.edit_text(
        f"🎨 <b>{title}</b>\nВыберите кнопку для настройки "
        "(🟢 — видна клиентам, 🔴 — скрыта):",
        reply_markup=kb.as_markup(),
    )


async def _show_button(call: CallbackQuery, key: str) -> None:
    r = menu_service.resolved(key)
    e = menu_service.get_entry(key)
    if not r or not e:
        await call.answer("Не найдено", show_alert=True)
        return
    emoji_state = "🙂 есть" if r.emoji_id else "— нет"
    vis = "🟢 видна" if r.enabled else "🔴 скрыта"
    color_name = _MENU_COLOR_NAME.get(r.style, "без цвета")
    caption = (
        f"🔧 <b>Кнопка</b>\n"
        f"Меню: {menu_service.MENU_TITLES.get(e.menu, e.menu)}\n"
        f"Текст: <b>{r.text}</b>\n"
        f"Эмодзи: {emoji_state}\n"
        f"Цвет: {color_name}\n"
        f"Видимость: {vis}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(
        _btn("✏️ Текст", f"ap_lbl:{key}"),
        _btn("🙂 Эмодзи", f"ap_emo:{key}"),
    )
    kb.row(_btn(f"🎨 Цвет: {color_name}", f"ap_color:{key}"))
    kb.row(
        _btn("🔼 Вверх", f"ap_up:{key}"),
        _btn("🔽 Вниз", f"ap_dn:{key}"),
    )
    tog = "🔴 Скрыть" if r.enabled else "🟢 Показать"
    kb.row(_btn(tog, f"ap_tog:{key}"))
    kb.row(_btn("⬅️ Назад", f"ap_menu:{e.menu}"))
    await call.message.edit_text(caption, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("ap_btn:"))
async def cb_button(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_button(call, call.data.split(":", 1)[1])
    await call.answer()


@router.callback_query(F.data.startswith("ap_tog:"))
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.toggle(db, key)
    await _show_button(call, key)
    await call.answer("Готово")


@router.callback_query(F.data.startswith("ap_color:"))
async def cb_menu_color(call: CallbackQuery, db: Database) -> None:
    key = call.data.split(":", 1)[1]
    r = menu_service.resolved(key)
    cur = (r.style or "") if r else ""
    nxt = _MENU_COLOR_CYCLE[(_MENU_COLOR_CYCLE.index(cur) + 1) % len(_MENU_COLOR_CYCLE)] \
        if cur in _MENU_COLOR_CYCLE else "primary"
    await menu_service.set_style(db, key, nxt)
    await _show_button(call, key)
    await call.answer(f"Цвет: {_MENU_COLOR_NAME.get(nxt)}")


@router.callback_query(F.data.startswith("ap_up:"))
async def cb_up(call: CallbackQuery, db: Database) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.move(db, key, -1)
    await _show_button(call, key)
    await call.answer("⬆️")


@router.callback_query(F.data.startswith("ap_dn:"))
async def cb_down(call: CallbackQuery, db: Database) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.move(db, key, +1)
    await _show_button(call, key)
    await call.answer("⬇️")


# ── Кнопки каталога: цвет, ширина, порядок (Product.*) ────────────────────────
# "" — без цвета (обычная кнопка); None — не задан (каталог рисует синим).
_CAT_STYLE_CYCLE = ["", "primary", "success", "danger"]
_CAT_COLOR = {"": "⚪ без цвета", "primary": "🔵 синий",
              "success": "🟢 зелёный", "danger": "🔴 красный"}
_CAT_DOT = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


def _cat_dot(style) -> str:
    if style is None:
        return "🔵"   # не задан = синий по умолчанию (как рисует каталог)
    return _CAT_DOT.get(style, "⚪")   # "" → без цвета


def _cat_color_name(style) -> str:
    key = "primary" if style is None else style
    return _CAT_COLOR.get(key, _CAT_COLOR["primary"])


def _cat_next_style(cur) -> str:
    base = "primary" if cur is None else cur
    if base not in _CAT_STYLE_CYCLE:
        base = "primary"
    return _CAT_STYLE_CYCLE[(_CAT_STYLE_CYCLE.index(base) + 1) % len(_CAT_STYLE_CYCLE)]


async def _show_catalog_list(call: CallbackQuery, db: Database, parent_id) -> None:
    async with db.session() as session:
        products = await catalog_service.list_products(
            session, only_active=False, parent_id=parent_id
        )
        parent = await session.get(Product, parent_id) if parent_id else None
    kb = InlineKeyboardBuilder()
    for p in products:
        width = "↔️" if p.full_width else "▭"
        hidden = "" if p.is_active else " 🚫"
        kb.row(_btn(f"{_cat_dot(p.button_style)} {width} {p.title}{hidden}", f"apc_btn:{p.id}"))
    back = f"apc_btn:{parent.id}" if parent else "ap_home"
    kb.row(_btn("⬅️ Назад", back))
    where = f" «{parent.title}»" if parent else ""
    await call.message.edit_text(
        f"🛍 <b>Кнопки каталога{where}</b>\n\n"
        "Порядок сверху вниз = порядок в каталоге. "
        "🔵/🟢/🔴 — цвет, ↔️ — во всю строку, ▭ — половина, 🚫 — скрыт.\n\n"
        "Выберите кнопку для настройки:",
        reply_markup=kb.as_markup(),
    )


async def _show_catalog_btn(call: CallbackQuery, db: Database, pid: int) -> None:
    async with db.session() as session:
        p = await session.get(Product, pid)
        if not p:
            await call.answer("Не найдено", show_alert=True)
            return
        children = await catalog_service.list_products(
            session, only_active=False, parent_id=pid
        )
        variants = [] if children else await catalog_service.list_variants(
            session, pid, only_active=False
        )
        parent_id = p.parent_id
        title = p.title
        style = p.button_style
        full_width = bool(p.full_width)
        has_children = bool(children)
        has_variants = bool(variants)
    fw_state = "↔️ во всю строку" if full_width else "▭ половина строки"
    color_name = _cat_color_name(style)
    caption = (
        f"🛍 <b>Кнопка каталога</b>\n"
        f"Товар: <b>{title}</b>\n"
        f"Цвет: {color_name}\n"
        f"Ширина: {fw_state}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"🎨 Цвет: {color_name}", f"apc_color:{pid}"))
    fw_lbl = "▭ Сделать половину" if full_width else "↔️ Во всю строку"
    kb.row(_btn(fw_lbl, f"apc_fw:{pid}"))
    kb.row(_btn("🔼 Вверх", f"apc_up:{pid}"), _btn("🔽 Вниз", f"apc_dn:{pid}"))
    if has_children:
        kb.row(_btn("📂 Кнопки внутри", f"apc_cat:{pid}"))
    elif has_variants:
        kb.row(_btn("📂 Номиналы (ширина/порядок)", f"apc_vars:{pid}"))
    back = f"apc_cat:{parent_id}" if parent_id else "apc_home"
    kb.row(_btn("⬅️ Назад", back))
    await call.message.edit_text(caption, reply_markup=kb.as_markup())


@router.callback_query(F.data == "apc_home")
async def cb_cat_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_catalog_list(call, db, None)
    await call.answer()


@router.callback_query(F.data.startswith("apc_cat:"))
async def cb_cat_level(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    await _show_catalog_list(call, db, pid)
    await call.answer()


@router.callback_query(F.data.startswith("apc_btn:"))
async def cb_cat_btn(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    await _show_catalog_btn(call, db, pid)
    await call.answer()


@router.callback_query(F.data.startswith("apc_color:"))
async def cb_cat_color(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.button_style = _cat_next_style(p.button_style)
            await session.commit()
            name = _cat_color_name(p.button_style)
        else:
            name = ""
    await _show_catalog_btn(call, db, pid)
    await call.answer(f"Цвет: {name}" if name else "Цвет изменён")


@router.callback_query(F.data.startswith("apc_fw:"))
async def cb_cat_fw(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.full_width = not bool(p.full_width)
            await session.commit()
    await _show_catalog_btn(call, db, pid)
    await call.answer("Ширина изменена")


# ── Оформление номиналов (ширина/порядок) ───────────────────────────────────
async def _show_variants_list(call: CallbackQuery, db: Database, pid: int) -> None:
    async with db.session() as session:
        p = await session.get(Product, pid)
        variants = await catalog_service.list_variants(session, pid, only_active=False)
    if not p:
        await call.answer("Не найдено", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for v in variants:
        width = "↔️" if v.full_width else "▭"
        hidden = "" if v.is_active else " 🚫"
        kb.row(_btn(f"{width} {v.title}{hidden}", f"apc_vbtn:{v.id}"))
    kb.row(_btn("⬅️ Назад", f"apc_btn:{pid}"))
    await call.message.edit_text(
        f"🧩 <b>Номиналы «{p.title}»</b>\n\n"
        "Порядок сверху вниз = порядок в каталоге. "
        "↔️ — во всю строку, ▭ — половина (сетка по 2), 🚫 — выключен.\n\n"
        "Выберите номинал:",
        reply_markup=kb.as_markup(),
    )


async def _show_variant_btn(call: CallbackQuery, db: Database, vid: int) -> None:
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await call.answer("Не найдено", show_alert=True)
            return
        pid, title, fw = v.product_id, v.title, bool(v.full_width)
    fw_state = "↔️ во всю строку" if fw else "▭ половина строки"
    kb = InlineKeyboardBuilder()
    kb.row(_btn("▭ Сделать половину" if fw else "↔️ Во всю строку", f"apc_vfw:{vid}"))
    kb.row(_btn("🔼 Вверх", f"apc_vup:{vid}"), _btn("🔽 Вниз", f"apc_vdn:{vid}"))
    kb.row(_btn("⬅️ Назад", f"apc_vars:{pid}"))
    await call.message.edit_text(
        f"🧩 <b>Номинал</b>\nНоминал: <b>{title}</b>\nШирина: {fw_state}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("apc_vars:"))
async def cb_var_list(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    await _show_variants_list(call, db, pid)
    await call.answer()


@router.callback_query(F.data.startswith("apc_vbtn:"))
async def cb_var_btn(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    await _show_variant_btn(call, db, vid)
    await call.answer()


@router.callback_query(F.data.startswith("apc_vfw:"))
async def cb_var_fw(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.full_width = not bool(v.full_width)
            await session.commit()
    await _show_variant_btn(call, db, vid)
    await call.answer("Ширина изменена")


@router.callback_query(F.data.startswith("apc_vup:"))
async def cb_var_up(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        await catalog_service.move_variant(session, vid, -1)
    await _show_variant_btn(call, db, vid)
    await call.answer("Перемещено вверх")


@router.callback_query(F.data.startswith("apc_vdn:"))
async def cb_var_dn(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        await catalog_service.move_variant(session, vid, 1)
    await _show_variant_btn(call, db, vid)
    await call.answer("Перемещено вниз")


@router.callback_query(F.data.startswith("apc_up:"))
async def cb_cat_up(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        moved = await catalog_service.move_product(session, pid, -1)
    await _show_catalog_btn(call, db, pid)
    await call.answer("⬆️" if moved else "Уже вверху")


@router.callback_query(F.data.startswith("apc_dn:"))
async def cb_cat_down(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        moved = await catalog_service.move_product(session, pid, +1)
    await _show_catalog_btn(call, db, pid)
    await call.answer("⬇️" if moved else "Уже внизу")


# ── Текст кнопки ─────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ap_lbl:"))
async def cb_label_ask(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    r = menu_service.resolved(key)
    await state.set_state(APState.set_label)
    await state.update_data(key=key)
    await call.message.edit_text(
        f"✏️ Текущий текст: <b>{r.text if r else ''}</b>\n\n"
        "Пришлите новый текст кнопки.\n<code>0</code> — вернуть по умолчанию.",
        reply_markup=_cancel(f"ap_btn:{key}"),
    )
    await call.answer()


@router.message(APState.set_label)
async def msg_label(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("key")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    await menu_service.set_label(db, key, None if raw == "0" else raw[:255])
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К кнопке", f"ap_btn:{key}")).as_markup()
    await message.answer("✅ Текст кнопки обновлён", reply_markup=kb)


# ── Эмодзи кнопки ────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("ap_emo:"))
async def cb_emoji_ask(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    await state.set_state(APState.set_emoji)
    await state.update_data(key=key)
    await call.message.edit_text(
        "🙂 Пришлите <b>премиум-эмодзи</b> (одним символом) — оно станет иконкой кнопки.\n"
        "<code>-</code> — убрать эмодзи.\n"
        "<code>0</code> — вернуть по умолчанию.",
        reply_markup=_cancel(f"ap_btn:{key}"),
    )
    await call.answer()


@router.message(APState.set_emoji)
async def msg_emoji(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    key = data.get("key")
    raw = (message.text or "").strip()
    emoji_id: str | None
    if raw == "0":
        emoji_id = None  # сброс к дефолту
    elif raw == "-":
        emoji_id = ""     # без эмодзи
    else:
        cid = None
        for ent in (message.entities or []):
            if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
                cid = ent.custom_emoji_id
                break
        if not cid:
            await message.answer(
                "Не нашёл премиум-эмодзи в сообщении. Пришлите именно премиум-эмодзи, "
                "либо <code>-</code> (убрать) / <code>0</code> (по умолчанию)."
            )
            return
        emoji_id = cid
    await menu_service.set_emoji(db, key, emoji_id)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К кнопке", f"ap_btn:{key}")).as_markup()
    await message.answer("✅ Эмодзи кнопки обновлено", reply_markup=kb)


# ── Тексты экранов (любой экран) ─────────────────────────────────────────────
@router.callback_query(F.data == "ap_texts")
async def cb_texts(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, title, _hint in menu_service.SCREEN_TEXTS:
        kb.row(_btn(f"📝 {title}", f"ap_cap:{key}"))
    kb.row(_btn("⬅️ Назад", "ap_home"))
    await call.message.edit_text(
        "📝 <b>Тексты экранов</b>\n\nВыберите экран, чтобы изменить его текст.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ap_cap:"))
async def cb_caption_ask(call: CallbackQuery, state: FSMContext) -> None:
    screen = call.data.split(":", 1)[1]
    title = menu_service.SCREEN_TITLES.get(screen, screen)
    hint = menu_service.SCREEN_HINTS.get(screen, "")
    await state.set_state(APState.set_caption)
    await state.update_data(menu=screen)
    extra = (f"\n\nПодстановки: <code>{hint}</code> — вставьте в текст для значений."
             if hint else "")
    await call.message.edit_text(
        f"📝 <b>{title}</b>\n\nПришлите новый текст экрана (можно премиум-эмодзи и "
        f"форматирование).\n<code>0</code> — вернуть текст по умолчанию.{extra}",
        reply_markup=_cancel("ap_texts"),
    )
    await call.answer()


@router.message(APState.set_caption)
async def msg_caption(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    menu = data.get("menu")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    value = None if raw == "0" else (message.html_text or message.text).strip()
    await menu_service.set_caption(db, menu, value)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ Назад", "ap_texts")).as_markup()
    await message.answer("✅ Текст экрана обновлён", reply_markup=kb)


# ── Способы оплаты (вкл/выкл + текст кнопок + заголовок экрана) ───────────────
def _provider_ok(config: BotConfig, provider_name: str) -> bool:
    return {
        "payhot": config.payhot_enabled,
        "paydex": config.paydex_enabled,
        "bolt": True,  # крипта всегда доступна (реальный провайдер или mock)
    }.get(provider_name, False)


async def _show_pay_home(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    async with db.session() as session:
        methods = await paymethods_service.list_methods(session)
    kb = InlineKeyboardBuilder()
    for m in methods:
        mark = "🟢" if m["enabled"] else "🔴"
        warn = "" if _provider_ok(config, m["provider"]) else " ⚠️"
        kb.row(_btn(f"{mark} {m['label']}{warn}", f"pm:{m['id']}"))
    kb.row(_btn("📝 Заголовок экрана", "pm_head"))
    kb.row(_btn("⬅️ Назад", "ap_home"))
    await call.message.edit_text(
        "💳 <b>Способы оплаты</b>\n\n"
        "Кнопки на экране пополнения баланса. 🟢 — видна клиентам, 🔴 — скрыта.\n"
        "⚠️ — провайдер не настроен (кнопку покажу только когда настроишь ключи).\n\n"
        "Выберите способ для настройки:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "pm_home")
async def cb_pay_home(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext
) -> None:
    await state.clear()
    await _show_pay_home(call, db, config)
    await call.answer()


async def _show_pay_method(call: CallbackQuery, db: Database, config: BotConfig, mid: str) -> None:
    if mid not in paymethods_service.METHOD_IDS:
        await call.answer("Не найдено", show_alert=True)
        return
    async with db.session() as session:
        enabled = await paymethods_service.is_enabled(session, mid)
        label = await paymethods_service.label(session, mid)
    provider = paymethods_service.PROVIDER_OF[mid]
    ok = _provider_ok(config, provider)
    vis = "🟢 видна" if enabled else "🔴 скрыта"
    prov_state = "✅ настроен" if ok else "⚠️ не настроен (нет ключей)"
    caption = (
        f"💳 <b>{paymethods_service.TITLE.get(mid, mid)}</b>\n\n"
        f"Текст кнопки: <b>{label}</b>\n"
        f"Видимость: {vis}\n"
        f"Провайдер: {prov_state}"
    )
    if enabled and not ok:
        caption += "\n\n❗️ Кнопка включена, но провайдер не настроен — клиенту она не показывается."
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✏️ Текст кнопки", f"pm_lbl:{mid}"))
    tog = "🔴 Скрыть" if enabled else "🟢 Показать"
    kb.row(_btn(tog, f"pm_tog:{mid}"))
    kb.row(_btn("⬅️ Назад", "pm_home"))
    await call.message.edit_text(caption, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("pm:"))
async def cb_pay_method(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext
) -> None:
    await state.clear()
    await _show_pay_method(call, db, config, call.data.split(":", 1)[1])
    await call.answer()


@router.callback_query(F.data.startswith("pm_tog:"))
async def cb_pay_toggle(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    mid = call.data.split(":", 1)[1]
    if mid not in paymethods_service.METHOD_IDS:
        await call.answer("Не найдено", show_alert=True)
        return
    async with db.session() as session:
        cur = await paymethods_service.is_enabled(session, mid)
        await paymethods_service.set_enabled(session, mid, not cur)
        await session.commit()
    await _show_pay_method(call, db, config, mid)
    await call.answer("Готово")


@router.callback_query(F.data.startswith("pm_lbl:"))
async def cb_pay_label_ask(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    mid = call.data.split(":", 1)[1]
    if mid not in paymethods_service.METHOD_IDS:
        await call.answer("Не найдено", show_alert=True)
        return
    async with db.session() as session:
        label = await paymethods_service.label(session, mid)
    await state.set_state(APState.set_pay_label)
    await state.update_data(pay_mid=mid)
    await call.message.edit_text(
        f"✏️ Текущий текст кнопки: <b>{label}</b>\n\n"
        "Пришлите новый текст кнопки.\n<code>0</code> — вернуть по умолчанию.",
        reply_markup=_cancel(f"pm:{mid}"),
    )
    await call.answer()


@router.message(APState.set_pay_label)
async def msg_pay_label(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    mid = data.get("pay_mid")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    async with db.session() as session:
        await paymethods_service.set_label(session, mid, None if raw == "0" else raw[:64])
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К способу", f"pm:{mid}")).as_markup()
    await message.answer("✅ Текст кнопки обновлён", reply_markup=kb)


@router.callback_query(F.data == "pm_head")
async def cb_pay_heading_ask(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    async with db.session() as session:
        heading = await paymethods_service.heading(session)
    await state.set_state(APState.set_pay_heading)
    await call.message.edit_text(
        f"📝 Текущий заголовок экрана оплаты:\n\n{heading}\n\n"
        "Пришлите новый заголовок (можно премиум-эмодзи и форматирование).\n"
        "<code>0</code> — вернуть по умолчанию.",
        reply_markup=_cancel("pm_home"),
    )
    await call.answer()


@router.message(APState.set_pay_heading)
async def msg_pay_heading(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    value = None if raw == "0" else (message.html_text or message.text).strip()
    async with db.session() as session:
        await paymethods_service.set_heading(session, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_btn("⬅️ Назад", "pm_home")).as_markup()
    await message.answer("✅ Заголовок экрана обновлён", reply_markup=kb)

"""Кабинет мерчанта (P1.3): владелец франшизы управляет наценкой и видит доход.

Доступен ТОЛЬКО в боте франшизы (merchant_id > 0) её владельцу
(``Merchant.admin_user_id``). В головном боте раздел не показывается.

Возможности:
  * статистика (клиенты, заказы) и баланс заработка (накопленная маржа);
  * наценка по умолчанию (%) на весь каталог;
  * индивидуальная наценка %/фикс-цена на конкретный номинал (поверх общей).

Запись цен — через ``services.mprice`` (стоп-лосс: не ниже цены владельца).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Merchant
from ..services import broadcast as broadcast_service
from ..services import catalog as catalog_service
from ..services import menu as menu_service
from ..services import merchants as merchants_service
from ..services import mprice as mprice_service
from ..services import msettings as msettings_service
from ..services.pricing import price_rub_value
from .. import texts
from .. import ui as ui_service

# Меню, которые владелец франшизы может оформлять (earn.franchise — служебная,
# в боте франшизы не показывается, поэтому из редактора исключаем).
APP_MENUS = ["main", "profile", "faq", "earn"]
_HIDDEN_KEYS = {"earn.franchise"}

# Экраны-баннеры (имя → подпись) и контакты (ключ → подпись).
BANNER_SCREENS = [
    ("main", "Главный"), ("catalog", "Каталог"), ("profile", "Профиль"),
    ("reviews", "Отзывы"), ("faq", "FAQ"),
]
CONTACTS = [
    (msettings_service.SUPPORT, "Техподдержка"),
    (msettings_service.URL_OFFER, "Оферта"),
    (msettings_service.URL_AGREEMENT, "Соглашение"),
    (msettings_service.URL_PRIVACY, "Конфиденциальность"),
]


def _norm_contact(raw: str) -> str:
    """Нормализовать контакт: @username/username → https://t.me/..., url — как есть."""
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    if raw.startswith("@"):
        return "https://t.me/" + raw[1:]
    return "https://t.me/" + raw

router = Router(name="merchant_admin")

# Максимальная наценка (%) — защита от опечатки вроде 100000%.
MAX_MARKUP = Decimal("100000")
MAX_FIXED = Decimal("10000000")


class IsMerchantAdminCb(BaseFilter):
    async def __call__(
        self, call: CallbackQuery, db: Database, merchant_id: int = HEAD_MERCHANT_ID
    ) -> bool:
        if merchant_id == HEAD_MERCHANT_ID or not call.from_user:
            return False
        return await merchants_service.admin_merchant(db, call.from_user.id, merchant_id) is not None


class IsMerchantAdminMsg(BaseFilter):
    async def __call__(
        self, message: Message, db: Database, merchant_id: int = HEAD_MERCHANT_ID
    ) -> bool:
        if merchant_id == HEAD_MERCHANT_ID or not message.from_user:
            return False
        return await merchants_service.admin_merchant(db, message.from_user.id, merchant_id) is not None


router.callback_query.filter(IsMerchantAdminCb())
router.message.filter(IsMerchantAdminMsg())


class MerchAdmin(StatesGroup):
    set_default = State()
    set_markup = State()   # data: vid
    set_fixed = State()    # data: vid
    set_caption = State()  # data: menu
    set_label = State()    # data: key
    set_emoji = State()    # data: key
    set_banner = State()   # data: banner
    set_contact = State()  # data: ckey
    bc_compose = State()   # рассылка: ввод сообщения


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _num(raw: str) -> Decimal | None:
    try:
        v = Decimal((raw or "").strip().replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None
    return v if v >= 0 else None


async def _edit(call: CallbackQuery, caption: str, kb: InlineKeyboardBuilder) -> None:
    try:
        await call.message.edit_text(caption, reply_markup=kb.as_markup())
    except Exception:  # noqa: BLE001 — сообщение-фото/без изменений
        await call.message.answer(caption, reply_markup=kb.as_markup())


# ── Главная кабинета ──────────────────────────────────────────────────────────
async def _show_home(call: CallbackQuery, db: Database, merchant_id: int) -> None:
    async with db.session() as s:
        m = await s.get(Merchant, merchant_id)
    st = await merchants_service.merchant_stats(db, merchant_id)
    dm = Decimal(m.default_markup or 0) if m else Decimal(0)
    earn = Decimal(m.earnings_rub or 0) if m else Decimal(0)
    caption = (
        f"🏪 <b>Кабинет франшизы</b>\n"
        f"{m.title or '—'}\n\n"
        f"💰 Заработок: <b>{texts.rub(earn)}</b>\n"
        f"📈 Наценка по умолчанию: <b>{dm:g}%</b>\n\n"
        f"👤 Клиентов: <b>{st['users']}</b>\n"
        f"🧾 Заказов: <b>{st['orders']}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_b("📈 Наценка по умолчанию", "ma_setdef"))
    kb.row(_b("🏷 Наценка по номиналам", "ma_price"))
    kb.row(_b("🎨 Оформление", "mo_home"))
    kb.row(_b("📣 Рассылка", "mb_home"))
    kb.row(_b("⬅️ В меню", "menu"))
    await _edit(call, caption, kb)


@router.callback_query(F.data == "ma_home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext,
                  merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_home(call, db, merchant_id)
    await call.answer()


# ── Наценка по умолчанию ──────────────────────────────────────────────────────
@router.callback_query(F.data == "ma_setdef")
async def cb_setdef(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MerchAdmin.set_default)
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ Отмена", "ma_home"))
    await _edit(
        call,
        "📈 <b>Наценка по умолчанию</b>\n\n"
        "Введите процент наценки поверх цены владельца (например <code>20</code> — +20%).\n"
        "Применяется ко всем номиналам без индивидуальной цены.\n"
        "<code>0</code> — продавать по цене владельца (без заработка).",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_default)
async def msg_setdef(message: Message, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    val = _num(message.text or "")
    if val is None or val > MAX_MARKUP:
        await message.answer("Введите число ≥ 0 (например 20). Попробуйте ещё раз.")
        return
    async with db.session() as s:
        await mprice_service.set_default_markup(s, merchant_id, val)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ В кабинет", "ma_home"))
    await message.answer(
        f"✅ Наценка по умолчанию: <b>{val:g}%</b>", reply_markup=kb.as_markup()
    )


# ── Навигация по каталогу для ценообразования ─────────────────────────────────
async def _show_products(call: CallbackQuery, db: Database, parent_id: int | None) -> None:
    async with db.session() as s:
        products = await catalog_service.list_products(s, parent_id=parent_id)
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.row(_b(f"{p.title}", f"ma_prod:{p.id}"))
    kb.row(_b("⬅️ В кабинет", "ma_home"))
    caption = (
        "🏷 <b>Наценка по номиналам</b>\n\n"
        "Выберите товар, затем номинал. Индивидуальная цена важнее общей наценки."
        if products else "🏷 <b>Наценка по номиналам</b>\n\nКаталог пуст."
    )
    await _edit(call, caption, kb)


@router.callback_query(F.data == "ma_price")
async def cb_price_root(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_products(call, db, None)
    await call.answer()


@router.callback_query(F.data.startswith("ma_prod:"))
async def cb_prod(call: CallbackQuery, db: Database, config: BotConfig,
                  merchant_id: int = HEAD_MERCHANT_ID) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as s:
        children = await catalog_service.list_children(s, pid)
        if children:
            # подкатегории — углубляемся
            kb = InlineKeyboardBuilder()
            for p in children:
                kb.row(_b(f"{p.title}", f"ma_prod:{p.id}"))
            kb.row(_b("⬅️ Назад", "ma_price"))
            await _edit(call, "🏷 Выберите категорию:", kb)
            await call.answer()
            return
        variants = await catalog_service.list_variants(s, pid)
        owner_prices = {v.id: price_rub_value(v, config.default_markup_percent) for v in variants}
        client = await mprice_service.client_prices(s, merchant_id, owner_prices)
        overrides = {}
        for v in variants:
            overrides[v.id] = await mprice_service.get_override(s, merchant_id, v.id)
        titles = {v.id: v.title for v in variants}
    kb = InlineKeyboardBuilder()
    for vid in titles:
        tag = ""
        ov = overrides.get(vid)
        if ov is not None:
            tag = " 🏷%" if ov.mode == "markup" else " 🏷₽"
        kb.row(_b(f"{titles[vid]} — {texts.rub(client[vid])}{tag}", f"ma_var:{vid}"))
    kb.row(_b("⬅️ Назад", "ma_price"))
    await _edit(
        call,
        "🏷 <b>Номиналы</b>\n\nЦена показана как её видит клиент. 🏷 — задана "
        "индивидуальная цена. Нажмите номинал, чтобы изменить.",
        kb,
    )
    await call.answer()


async def _show_variant(call: CallbackQuery, db: Database, config: BotConfig,
                        merchant_id: int, vid: int) -> None:
    async with db.session() as s:
        variant = await catalog_service.get_variant(s, vid)
        if variant is None:
            await call.answer("Номинал не найден", show_alert=True)
            return
        owner = price_rub_value(variant, config.default_markup_percent)
        client = await mprice_service.client_price(s, merchant_id, vid, owner)
        ov = await mprice_service.get_override(s, merchant_id, vid)
        dm = await mprice_service.default_markup(s, merchant_id)
        pid = variant.product_id
        title = variant.title
    if ov is None:
        cur = f"по умолчанию (+{dm:g}%)"
    elif ov.mode == "markup":
        cur = f"наценка +{Decimal(ov.markup_percent or 0):g}%"
    else:
        cur = f"фикс {texts.rub(Decimal(ov.price_rub or 0))}"
    caption = (
        f"🏷 <b>{title}</b>\n\n"
        f"Цена владельца: <b>{texts.rub(owner)}</b>\n"
        f"Цена клиенту: <b>{texts.rub(client)}</b>\n"
        f"Правило: {cur}\n\n"
        "Фикс-цена не может быть ниже цены владельца (стоп-лосс)."
    )
    kb = InlineKeyboardBuilder()
    kb.row(_b("📈 Наценка %", f"ma_mk:{vid}"), _b("💵 Фикс ₽", f"ma_fx:{vid}"))
    if ov is not None:
        kb.row(_b("♻️ Сбросить к общей", f"ma_clr:{vid}"))
    kb.row(_b("⬅️ К номиналам", f"ma_prod:{pid}"))
    await _edit(call, caption, kb)


@router.callback_query(F.data.startswith("ma_var:"))
async def cb_var(call: CallbackQuery, db: Database, config: BotConfig,
                 merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await _show_variant(call, db, config, merchant_id, int(call.data.split(":", 1)[1]))
    await call.answer()


@router.callback_query(F.data.startswith("ma_clr:"))
async def cb_clear(call: CallbackQuery, db: Database, config: BotConfig,
                   merchant_id: int = HEAD_MERCHANT_ID) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as s:
        await mprice_service.clear_override(s, merchant_id, vid)
        await s.commit()
    await _show_variant(call, db, config, merchant_id, vid)
    await call.answer("Сброшено к общей наценке ✅")


@router.callback_query(F.data.startswith("ma_mk:"))
async def cb_ask_markup(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(MerchAdmin.set_markup)
    await state.update_data(vid=vid)
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ Отмена", f"ma_var:{vid}"))
    await _edit(
        call,
        "📈 Введите индивидуальную наценку в % для этого номинала "
        "(например <code>35</code>).",
        kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("ma_fx:"))
async def cb_ask_fixed(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(MerchAdmin.set_fixed)
    await state.update_data(vid=vid)
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ Отмена", f"ma_var:{vid}"))
    await _edit(
        call,
        "💵 Введите фикс-цену в рублях для этого номинала (например <code>499</code>).\n"
        "Если она ниже цены владельца — поднимется до неё автоматически.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_markup)
async def msg_set_markup(message: Message, db: Database, config: BotConfig,
                         state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    val = _num(message.text or "")
    if val is None or val > MAX_MARKUP:
        await message.answer("Введите число ≥ 0 (например 35). Попробуйте ещё раз.")
        return
    data = await state.get_data()
    vid = int(data.get("vid", 0))
    async with db.session() as s:
        await mprice_service.set_markup(s, merchant_id, vid, val)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ К номиналу", f"ma_var:{vid}"))
    await message.answer(
        f"✅ Наценка номинала: <b>+{val:g}%</b>", reply_markup=kb.as_markup()
    )


@router.message(MerchAdmin.set_fixed)
async def msg_set_fixed(message: Message, db: Database, config: BotConfig,
                        state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    val = _num(message.text or "")
    if val is None or val <= 0 or val > MAX_FIXED:
        await message.answer("Введите сумму в рублях больше 0 (например 499).")
        return
    data = await state.get_data()
    vid = int(data.get("vid", 0))
    async with db.session() as s:
        await mprice_service.set_fixed(s, merchant_id, vid, val)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_b("⬅️ К номиналу", f"ma_var:{vid}"))
    await message.answer(
        f"✅ Фикс-цена номинала: <b>{texts.rub(val)}</b>", reply_markup=kb.as_markup()
    )


# ══════════════════════════════════════════════════════════════════════════════
# Оформление франшизы (O2): тексты экранов + кнопки меню
# ══════════════════════════════════════════════════════════════════════════════
async def _show_oform_home(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(_b("📝 Тексты экранов", "mo_texts"))
    for menu in APP_MENUS:
        kb.row(_b(f"🔘 Кнопки: {menu_service.MENU_TITLES.get(menu, menu)}", f"mo_menu:{menu}"))
    kb.row(_b("🖼 Баннеры", "mo_banners"))
    kb.row(_b("📞 Контакты и ссылки", "mo_contacts"))
    kb.row(_b("♻️ Сбросить оформление", "mo_reset"))
    kb.row(_b("⬅️ В кабинет", "ma_home"))
    await _edit(
        call,
        "🎨 <b>Оформление</b>\n\nНастройте тексты экранов, кнопки меню, баннеры и "
        "контакты своего бота.",
        kb,
    )


# ── Тексты экранов (все экраны) ──────────────────────────────────────────────
async def _show_texts(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    for key, title, _hint in menu_service.SCREEN_TEXTS:
        kb.row(_b(f"📝 {title}", f"mo_cap:{key}"))
    kb.row(_b("⬅️ Назад", "mo_home"))
    await _edit(
        call,
        "📝 <b>Тексты экранов</b>\n\nВыберите экран, чтобы изменить его текст "
        "(тот, что показывается после нажатия кнопки).",
        kb,
    )


@router.callback_query(F.data == "mo_texts")
async def cb_oform_texts(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_texts(call)
    await call.answer()


@router.callback_query(F.data == "mo_home")
async def cb_oform_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_oform_home(call)
    await call.answer()


async def _show_menu(call: CallbackQuery, menu: str, merchant_id: int) -> None:
    kb = InlineKeyboardBuilder()
    for r in menu_service.menu_entries(menu, merchant_id, only_enabled=False):
        if r.key in _HIDDEN_KEYS:
            continue
        mark = "🟢" if r.enabled else "🔴"
        kb.row(_b(f"{mark} {r.text}", f"mo_btn:{r.key}"))
    kb.row(_b("⬅️ Назад", "mo_home"))
    title = menu_service.MENU_TITLES.get(menu, menu)
    await _edit(
        call,
        f"🎨 <b>{title}</b>\nВыберите кнопку (🟢 — видна клиентам, 🔴 — скрыта):",
        kb,
    )


@router.callback_query(F.data.startswith("mo_menu:"))
async def cb_oform_menu(call: CallbackQuery, state: FSMContext,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_menu(call, call.data.split(":", 1)[1], merchant_id)
    await call.answer()


async def _show_button(call: CallbackQuery, key: str, merchant_id: int) -> None:
    r = menu_service.resolved(key, merchant_id)
    e = menu_service.get_entry(key)
    if not r or not e:
        await call.answer("Не найдено", show_alert=True)
        return
    emoji_state = "🙂 есть" if r.emoji_id else "— нет"
    vis = "🟢 видна" if r.enabled else "🔴 скрыта"
    caption = (
        f"🔧 <b>Кнопка</b>\n"
        f"Меню: {menu_service.MENU_TITLES.get(e.menu, e.menu)}\n"
        f"Текст: <b>{r.text}</b>\n"
        f"Эмодзи: {emoji_state}\n"
        f"Цвет: {_COLOR_NAME.get(r.style)}\n"
        f"Видимость: {vis}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_b("✏️ Текст", f"mo_lbl:{key}"), _b("🙂 Эмодзи", f"mo_emo:{key}"))
    kb.row(_b(f"🎨 Цвет: {_COLOR_NAME.get(r.style)}", f"mo_color:{key}"))
    kb.row(_b("🔼 Вверх", f"mo_up:{key}"), _b("🔽 Вниз", f"mo_dn:{key}"))
    tog = "🔴 Скрыть" if r.enabled else "🟢 Показать"
    kb.row(_b(tog, f"mo_tog:{key}"))
    kb.row(_b("⬅️ Назад", f"mo_menu:{e.menu}"))
    await _edit(call, caption, kb)


# Цикл цветов кнопки: без цвета → синий → зелёный → красный → ...
_COLOR_CYCLE = ["", "primary", "success", "danger"]
_COLOR_NAME = {None: "без цвета", "": "без цвета",
               "primary": "🔵 синий", "success": "🟢 зелёный", "danger": "🔴 красный"}


@router.callback_query(F.data.startswith("mo_color:"))
async def cb_oform_color(call: CallbackQuery, db: Database,
                         merchant_id: int = HEAD_MERCHANT_ID) -> None:
    key = call.data.split(":", 1)[1]
    r = menu_service.resolved(key, merchant_id)
    cur = r.style or "" if r else ""
    nxt = _COLOR_CYCLE[(_COLOR_CYCLE.index(cur) + 1) % len(_COLOR_CYCLE)] \
        if cur in _COLOR_CYCLE else "primary"
    await menu_service.set_style(db, key, nxt, merchant_id)
    await _show_button(call, key, merchant_id)
    await call.answer(f"Цвет: {_COLOR_NAME.get(nxt)}")


@router.callback_query(F.data.startswith("mo_btn:"))
async def cb_oform_button(call: CallbackQuery, state: FSMContext,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_button(call, call.data.split(":", 1)[1], merchant_id)
    await call.answer()


@router.callback_query(F.data.startswith("mo_tog:"))
async def cb_oform_toggle(call: CallbackQuery, db: Database,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.toggle(db, key, merchant_id)
    await _show_button(call, key, merchant_id)
    await call.answer("Готово")


@router.callback_query(F.data.startswith("mo_up:"))
async def cb_oform_up(call: CallbackQuery, db: Database,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.move(db, key, -1, merchant_id)
    await _show_button(call, key, merchant_id)
    await call.answer("⬆️")


@router.callback_query(F.data.startswith("mo_dn:"))
async def cb_oform_down(call: CallbackQuery, db: Database,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    key = call.data.split(":", 1)[1]
    await menu_service.move(db, key, +1, merchant_id)
    await _show_button(call, key, merchant_id)
    await call.answer("⬇️")


# ── Текст кнопки ─────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mo_lbl:"))
async def cb_oform_label_ask(call: CallbackQuery, state: FSMContext,
                             merchant_id: int = HEAD_MERCHANT_ID) -> None:
    key = call.data.split(":", 1)[1]
    r = menu_service.resolved(key, merchant_id)
    await state.set_state(MerchAdmin.set_label)
    await state.update_data(key=key)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", f"mo_btn:{key}"))
    await _edit(
        call,
        f"✏️ Текущий текст: <b>{r.text if r else ''}</b>\n\n"
        "Пришлите новый текст кнопки.\n<code>0</code> — вернуть по умолчанию.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_label)
async def msg_oform_label(message: Message, db: Database, state: FSMContext,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    key = data.get("key")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    await menu_service.set_label(db, key, None if raw == "0" else raw[:255], merchant_id)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ К кнопке", f"mo_btn:{key}")).as_markup()
    await message.answer("✅ Текст кнопки обновлён", reply_markup=kb)


# ── Эмодзи кнопки ────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("mo_emo:"))
async def cb_oform_emoji_ask(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    await state.set_state(MerchAdmin.set_emoji)
    await state.update_data(key=key)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", f"mo_btn:{key}"))
    await _edit(
        call,
        "🙂 Пришлите <b>премиум-эмодзи</b> (одним символом) — оно станет иконкой кнопки.\n"
        "<code>-</code> — убрать эмодзи.\n<code>0</code> — вернуть по умолчанию.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_emoji)
async def msg_oform_emoji(message: Message, db: Database, state: FSMContext,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    key = data.get("key")
    raw = (message.text or "").strip()
    if raw == "0":
        emoji_id = None
    elif raw == "-":
        emoji_id = ""
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
    await menu_service.set_emoji(db, key, emoji_id, merchant_id)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ К кнопке", f"mo_btn:{key}")).as_markup()
    await message.answer("✅ Эмодзи кнопки обновлено", reply_markup=kb)


# ── Текст экрана (любой экран) ───────────────────────────────────────────────
@router.callback_query(F.data.startswith("mo_cap:"))
async def cb_oform_caption_ask(call: CallbackQuery, state: FSMContext) -> None:
    screen = call.data.split(":", 1)[1]
    title = menu_service.SCREEN_TITLES.get(screen, screen)
    hint = menu_service.SCREEN_HINTS.get(screen, "")
    await state.set_state(MerchAdmin.set_caption)
    await state.update_data(menu=screen)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "mo_texts"))
    extra = (f"\n\nДоступные подстановки: <code>{hint}</code> — вставьте их в текст, "
             "чтобы показать значения." if hint else "")
    await _edit(
        call,
        f"📝 <b>{title}</b>\n\nПришлите новый текст экрана (можно премиум-эмодзи и "
        f"форматирование).\n<code>0</code> — вернуть текст по умолчанию.{extra}",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_caption)
async def msg_oform_caption(message: Message, db: Database, state: FSMContext,
                            merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    menu = data.get("menu")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0.")
        return
    value = None if raw == "0" else (message.html_text or message.text).strip()
    await menu_service.set_caption(db, menu, value, merchant_id)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ К текстам", "mo_texts")).as_markup()
    await message.answer("✅ Текст экрана обновлён", reply_markup=kb)


# ── Сброс оформления к шаблону ───────────────────────────────────────────────
@router.callback_query(F.data == "mo_reset")
async def cb_oform_reset_ask(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    kb.row(_b("♻️ Да, сбросить", "mo_reset_ok"))
    kb.row(_b("⬅️ Отмена", "mo_home"))
    await _edit(
        call,
        "♻️ Сбросить всё оформление к базовому шаблону?\n"
        "Тексты экранов и правки кнопок вернутся к стандартным. Баннеры не затрагиваются.",
        kb,
    )
    await call.answer()


@router.callback_query(F.data == "mo_reset_ok")
async def cb_oform_reset(call: CallbackQuery, db: Database, state: FSMContext,
                         merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await menu_service.reset_all(db, merchant_id)
    await _show_oform_home(call)
    await call.answer("Сброшено ✅")


# ══════════════════════════════════════════════════════════════════════════════
# Оформление франшизы (O3): баннеры-картинки + контакты/ссылки
# ══════════════════════════════════════════════════════════════════════════════
async def _show_banners(call: CallbackQuery, db: Database, merchant_id: int) -> None:
    async with db.session() as s:
        vals = await msettings_service.all_for(s, merchant_id)
    kb = InlineKeyboardBuilder()
    for name, title in BANNER_SCREENS:
        has = msettings_service.banner_key(name) in vals
        mark = "🖼" if has else "—"
        kb.row(_b(f"{mark} {title}", f"mo_bn:{name}"))
    kb.row(_b("⬅️ Назад", "mo_home"))
    await _edit(
        call,
        "🖼 <b>Баннеры</b>\n\nЗагрузите свою картинку для каждого экрана. "
        "Пока баннер не загружен — экран показывается без картинки (только текст).\n"
        "🖼 — свой баннер задан, — — без картинки.",
        kb,
    )


@router.callback_query(F.data == "mo_banners")
async def cb_banners(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_banners(call, db, merchant_id)
    await call.answer()


@router.callback_query(F.data.startswith("mo_bn:"))
async def cb_banner_one(call: CallbackQuery, db: Database, state: FSMContext,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    name = call.data.split(":", 1)[1]
    title = dict(BANNER_SCREENS).get(name, name)
    async with db.session() as s:
        cur = await msettings_service.get(s, merchant_id, msettings_service.banner_key(name))
    await state.set_state(MerchAdmin.set_banner)
    await state.update_data(banner=name)
    kb = InlineKeyboardBuilder()
    if cur:
        kb.row(_b("🗑 Убрать баннер", f"mo_bnclr:{name}"))
    kb.row(_b("⬅️ Назад", "mo_banners"))
    await _edit(
        call,
        f"🖼 <b>Баннер экрана «{title}»</b>\n\n"
        "Пришлите картинку (как фото) — она станет баннером этого экрана.\n"
        f"Статус: {'задан свой' if cur else 'без картинки'}.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_banner, F.photo)
async def msg_banner_photo(message: Message, db: Database, state: FSMContext,
                           merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    name = data.get("banner")
    file_id = message.photo[-1].file_id
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, msettings_service.banner_key(name), file_id)
        await s.commit()
    ui_service.set_banner_override(merchant_id, name, file_id)  # живой кэш рендера
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ К баннерам", "mo_banners")).as_markup()
    await message.answer("✅ Баннер обновлён", reply_markup=kb)


@router.message(MerchAdmin.set_banner)
async def msg_banner_notphoto(message: Message) -> None:
    await message.answer("Пришлите именно картинку (фото), либо вернитесь кнопкой «Назад».")


@router.callback_query(F.data.startswith("mo_bnclr:"))
async def cb_banner_clear(call: CallbackQuery, db: Database, state: FSMContext,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    name = call.data.split(":", 1)[1]
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, msettings_service.banner_key(name), None)
        await s.commit()
    ui_service.set_banner_override(merchant_id, name, None)
    await _show_banners(call, db, merchant_id)
    await call.answer("Баннер убран")


# ── Контакты и ссылки ────────────────────────────────────────────────────────
async def _show_contacts(call: CallbackQuery, db: Database, merchant_id: int) -> None:
    async with db.session() as s:
        vals = await msettings_service.all_for(s, merchant_id)
    kb = InlineKeyboardBuilder()
    lines = ["📞 <b>Контакты и ссылки</b>\n"]
    for key, title in CONTACTS:
        v = vals.get(key)
        lines.append(f"• {title}: {'<code>' + v + '</code>' if v else '—'}")
        kb.row(_b(f"✏️ {title}", f"mo_ct:{key}"))
    kb.row(_b("⬅️ Назад", "mo_home"))
    await _edit(call, "\n".join(lines), kb)


@router.callback_query(F.data == "mo_contacts")
async def cb_contacts(call: CallbackQuery, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_contacts(call, db, merchant_id)
    await call.answer()


@router.callback_query(F.data.startswith("mo_ct:"))
async def cb_contact_ask(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":", 1)[1]
    title = dict(CONTACTS).get(key, key)
    await state.set_state(MerchAdmin.set_contact)
    await state.update_data(ckey=key)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "mo_contacts"))
    hint = ("@username или ссылка t.me" if key == msettings_service.SUPPORT
            else "ссылка https://… ИЛИ текст документа (покажется прямо в боте)")
    await _edit(
        call,
        f"✏️ <b>{title}</b>\n\nПришлите значение ({hint}).\n"
        "<code>0</code> — убрать.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.set_contact)
async def msg_contact(message: Message, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    key = data.get("ckey")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите значение или 0.")
        return
    if raw == "0":
        value = None
    elif key == msettings_service.SUPPORT:
        value = _norm_contact(raw)
    else:
        value = raw  # ссылки оферты/политики — как есть
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, key, value)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ К контактам", "mo_contacts")).as_markup()
    await message.answer("✅ Сохранено", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
# Рассылка франшизы — только своим клиентам (через свой бот)
# ══════════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "mb_home")
async def cb_bc_home(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    count = len(await broadcast_service.recipients(db, merchant_id))
    kb = InlineKeyboardBuilder()
    kb.row(_b("✍️ Создать рассылку", "mb_new"))
    kb.row(_b("⬅️ В кабинет", "ma_home"))
    await _edit(
        call,
        "📣 <b>Рассылка</b>\n\n"
        f"Получатели: <b>{count}</b> клиентов вашего бота.\n"
        "Сообщение придёт только вашим клиентам, от вашего бота.",
        kb,
    )
    await call.answer()


@router.callback_query(F.data == "mb_new")
async def cb_bc_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MerchAdmin.bc_compose)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "mb_home"))
    await _edit(
        call,
        "✍️ Пришлите сообщение для рассылки.\n\n"
        "Можно текст (с форматированием и премиум-эмодзи) или фото с подписью.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.bc_compose)
async def msg_bc_compose(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1].file_id if message.photo else None
    # Текст/подпись с сохранением форматирования и премиум-эмодзи.
    text = message.html_text if (message.text or message.caption) else None
    if not photo and not text:
        await message.answer("Пришлите текст или фото. Попробуйте ещё раз.")
        return
    await state.update_data(bc_text=text, bc_photo=photo)
    await state.set_state(MerchAdmin.bc_compose)
    kb = InlineKeyboardBuilder()
    kb.row(_b("📤 Отправить всем", "mb_send"))
    kb.row(_b("⬅️ Отмена", "mb_home"))
    # Показать превью (как увидят клиенты).
    if photo:
        await message.answer_photo(photo, caption=text or None)
    elif text:
        await message.answer(text)
    await message.answer("👆 Так увидят клиенты. Отправить?", reply_markup=kb.as_markup())


@router.callback_query(F.data == "mb_send")
async def cb_bc_send(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    text, photo = data.get("bc_text"), data.get("bc_photo")
    await state.clear()
    if not text and not photo:
        await call.answer("Нечего отправлять", show_alert=True)
        return
    user_ids = await broadcast_service.recipients(db, merchant_id)
    bot = call.message.bot
    owner_id = call.from_user.id

    async def _job():
        sent, failed = await broadcast_service.run(bot, user_ids, text=text, photo=photo)
        try:
            await bot.send_message(
                owner_id,
                f"📣 Рассылка завершена.\nОтправлено: <b>{sent}</b> · "
                f"не доставлено: <b>{failed}</b>.",
            )
        except Exception:  # noqa: BLE001
            pass

    asyncio.create_task(_job())
    await _edit(
        call,
        f"📣 Рассылка запущена для <b>{len(user_ids)}</b> клиентов.\n"
        "По завершении пришлю отчёт.",
        InlineKeyboardBuilder().row(_b("⬅️ В кабинет", "ma_home")),
    )
    await call.answer("Запущено ✅")

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

from datetime import datetime, timedelta
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
from . import broadcast_flow
from ..services import catalog as catalog_service
from ..services import mcatalog as mcatalog_service
from ..services import menu as menu_service
from ..services import merchants as merchants_service
from ..services import mprice as mprice_service
from ..services import msettings as msettings_service
from ..services import payouts as payouts_service
from ..services import stats as stats_service
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
    oc_title = State()     # каталог: подпись (data: item_type, item_id)
    oc_emoji = State()     # каталог: эмодзи
    oc_desc = State()      # каталог: описание товара
    st_custom = State()    # статистика: свой период
    pf_sbp_phone = State()  # вывод: телефон СБП
    pf_sbp_bank = State()   # вывод: банк СБП (data: phone)
    pf_usdt = State()       # вывод: адрес USDT
    pf_amount = State()     # вывод: сумма (data: method)


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
    kb.row(_b("📊 Статистика", "fm_home"))
    kb.row(_b("📈 Наценка по умолчанию", "ma_setdef"))
    kb.row(_b("🏷 Наценка по номиналам", "ma_price"))
    kb.row(_b("🎨 Оформление", "mo_home"))
    kb.row(_b("📣 Рассылка", "mb:home"))
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
    kb.row(_b("🛍 Каталог (товары/номиналы)", "oc_home"))
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
# Рассылка франшизы — свой движок (только своим клиентам, через свой бот).
# Хендлеры регистрируются общим движком в конце модуля (prefix "mb").
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Оформление каталога франшизы: подпись/эмодзи/описание товаров и номиналов
# ══════════════════════════════════════════════════════════════════════════════
def _emoji_from_message(message: Message):
    """Вернуть custom_emoji_id из сообщения, либо спец-значения: '0'/'-'/None."""
    raw = (message.text or "").strip()
    if raw == "0":
        return "__reset__"
    if raw == "-":
        return ""
    for ent in (message.entities or []):
        if ent.type == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            return ent.custom_emoji_id
    return None


async def _show_oc_products(call: CallbackQuery, db: Database, merchant_id: int,
                            parent_id=None) -> None:
    async with db.session() as s:
        products = await catalog_service.list_products(
            s, parent_id=parent_id, exclude_head_only=merchant_id != HEAD_MERCHANT_ID
        )
        ov = await mcatalog_service.overrides_map(
            s, merchant_id, mcatalog_service.PRODUCT, [p.id for p in products]
        )
        rows = [(p.id, mcatalog_service.disp_title(p, ov.get(p.id))) for p in products]
    kb = InlineKeyboardBuilder()
    for pid, title in rows:
        kb.row(_b(title, f"oc_prod:{pid}"))
    kb.row(_b("⬅️ Назад", "mo_home"))
    await _edit(
        call,
        "🛍 <b>Каталог</b>\n\nВыберите товар/категорию, чтобы изменить подпись, "
        "эмодзи или описание в вашем боте. Цены общего каталога не меняются.",
        kb,
    )


@router.callback_query(F.data == "oc_home")
async def cb_oc_home(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_oc_products(call, db, merchant_id)
    await call.answer()


async def _show_oc_product(call: CallbackQuery, db: Database, merchant_id: int, pid: int) -> None:
    async with db.session() as s:
        product = await catalog_service.get_product(s, pid)
        if product is None:
            await call.answer("Не найдено", show_alert=True)
            return
        ov = await mcatalog_service.get_override(s, merchant_id, mcatalog_service.PRODUCT, pid)
        title = mcatalog_service.disp_title(product, ov)
        emoji = "🙂 есть" if mcatalog_service.disp_emoji(product, ov) else "— нет"
        desc = mcatalog_service.disp_description(product, ov)
        children = await catalog_service.list_children(
            s, pid, exclude_head_only=merchant_id != HEAD_MERCHANT_ID
        )
        variants = await catalog_service.list_variants(s, pid)
        ch = [(c.id, mcatalog_service.disp_title(c, None)) for c in children]
        vs = []
        if not children:
            vov = await mcatalog_service.overrides_map(
                s, merchant_id, mcatalog_service.VARIANT, [v.id for v in variants]
            )
            vs = [(v.id, mcatalog_service.disp_title(v, vov.get(v.id))) for v in variants]
        has_override = ov is not None
    caption = (
        f"🛍 <b>{title}</b>\n"
        f"Эмодзи: {emoji}\n"
        f"Описание: {'задано' if desc else '— нет'}"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_b("✏️ Подпись", f"oc_t:product:{pid}"), _b("🙂 Эмодзи", f"oc_e:product:{pid}"))
    kb.row(_b("📝 Описание", f"oc_d:{pid}"))
    if has_override:
        kb.row(_b("♻️ Сбросить", f"oc_clr:product:{pid}"))
    for cid, ctitle in ch:
        kb.row(_b(f"📁 {ctitle}", f"oc_prod:{cid}"))
    for vid, vtitle in vs:
        kb.row(_b(f"🧩 {vtitle}", f"oc_var:{vid}"))
    back = f"oc_prod:{product.parent_id}" if product.parent_id else "oc_home"
    kb.row(_b("⬅️ Назад", back))
    await _edit(call, caption, kb)


@router.callback_query(F.data.startswith("oc_prod:"))
async def cb_oc_product(call: CallbackQuery, db: Database, state: FSMContext,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_oc_product(call, db, merchant_id, int(call.data.split(":", 1)[1]))
    await call.answer()


async def _show_oc_variant(call: CallbackQuery, db: Database, merchant_id: int, vid: int) -> None:
    async with db.session() as s:
        variant = await catalog_service.get_variant(s, vid)
        if variant is None:
            await call.answer("Не найдено", show_alert=True)
            return
        ov = await mcatalog_service.get_override(s, merchant_id, mcatalog_service.VARIANT, vid)
        title = mcatalog_service.disp_title(variant, ov)
        emoji = "🙂 есть" if mcatalog_service.disp_emoji(variant, ov) else "— нет"
        pid = variant.product_id
        has_override = ov is not None
    caption = f"🧩 <b>{title}</b>\nЭмодзи: {emoji}"
    kb = InlineKeyboardBuilder()
    kb.row(_b("✏️ Подпись", f"oc_t:variant:{vid}"), _b("🙂 Эмодзи", f"oc_e:variant:{vid}"))
    if has_override:
        kb.row(_b("♻️ Сбросить", f"oc_clr:variant:{vid}"))
    kb.row(_b("⬅️ Назад", f"oc_prod:{pid}"))
    await _edit(call, caption, kb)


@router.callback_query(F.data.startswith("oc_var:"))
async def cb_oc_variant(call: CallbackQuery, db: Database, state: FSMContext,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_oc_variant(call, db, merchant_id, int(call.data.split(":", 1)[1]))
    await call.answer()


# ── Ввод: подпись / эмодзи / описание ────────────────────────────────────────
def _oc_back(it: str, iid: int) -> str:
    return f"oc_var:{iid}" if it == "variant" else f"oc_prod:{iid}"


@router.callback_query(F.data.startswith("oc_t:"))
async def cb_oc_title_ask(call: CallbackQuery, state: FSMContext) -> None:
    _, it, iid = call.data.split(":", 2)
    await state.set_state(MerchAdmin.oc_title)
    await state.update_data(it=it, iid=int(iid))
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", _oc_back(it, int(iid))))
    await _edit(call, "✏️ Пришлите новую подпись (кнопку).\n<code>0</code> — вернуть из каталога.", kb)
    await call.answer()


@router.message(MerchAdmin.oc_title)
async def msg_oc_title(message: Message, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
    d = await state.get_data(); it, iid = d.get("it"), int(d.get("iid"))
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст или 0."); return
    async with db.session() as s:
        await mcatalog_service.set_title(s, merchant_id, it, iid, None if raw == "0" else raw[:255])
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", _oc_back(it, iid))).as_markup()
    await message.answer("✅ Подпись обновлена", reply_markup=kb)


@router.callback_query(F.data.startswith("oc_e:"))
async def cb_oc_emoji_ask(call: CallbackQuery, state: FSMContext) -> None:
    _, it, iid = call.data.split(":", 2)
    await state.set_state(MerchAdmin.oc_emoji)
    await state.update_data(it=it, iid=int(iid))
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", _oc_back(it, int(iid))))
    await _edit(
        call,
        "🙂 Пришлите <b>премиум-эмодзи</b> — станет иконкой.\n"
        "<code>-</code> — убрать, <code>0</code> — вернуть из каталога.",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.oc_emoji)
async def msg_oc_emoji(message: Message, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
    d = await state.get_data(); it, iid = d.get("it"), int(d.get("iid"))
    val = _emoji_from_message(message)
    if val is None:
        await message.answer("Пришлите премиум-эмодзи, либо - (убрать) / 0 (из каталога)."); return
    emoji_id = None if val == "__reset__" else val
    async with db.session() as s:
        await mcatalog_service.set_emoji(s, merchant_id, it, iid, emoji_id)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", _oc_back(it, iid))).as_markup()
    await message.answer("✅ Эмодзи обновлено", reply_markup=kb)


@router.callback_query(F.data.startswith("oc_d:"))
async def cb_oc_desc_ask(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(MerchAdmin.oc_desc)
    await state.update_data(it="product", iid=pid)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", f"oc_prod:{pid}"))
    await _edit(call, "📝 Пришлите описание товара.\n<code>0</code> — вернуть из каталога, "
                      "<code>-</code> — убрать.\n\n💡 Курс с вашей наценкой: <code>{курс:1.38}</code> "
                      "(покажется базовое число × ваша наценка).", kb)
    await call.answer()


@router.message(MerchAdmin.oc_desc)
async def msg_oc_desc(message: Message, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    d = await state.get_data(); pid = int(d.get("iid"))
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст, 0 или -."); return
    if raw == "0":
        value = None
    elif raw == "-":
        value = ""
    else:
        value = (message.html_text or message.text).strip()
    async with db.session() as s:
        await mcatalog_service.set_description(s, merchant_id, "product", pid, value)
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", f"oc_prod:{pid}")).as_markup()
    await message.answer("✅ Описание обновлено", reply_markup=kb)


@router.callback_query(F.data.startswith("oc_clr:"))
async def cb_oc_clear(call: CallbackQuery, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    _, it, iid = call.data.split(":", 2)
    iid = int(iid)
    async with db.session() as s:
        await mcatalog_service.clear(s, merchant_id, it, iid)
        await s.commit()
    if it == "variant":
        await _show_oc_variant(call, db, merchant_id, iid)
    else:
        await _show_oc_product(call, db, merchant_id, iid)
    await call.answer("Сброшено ✅")


# ══════════════════════════════════════════════════════════════════════════════
# Статистика франшизы — в разрезе её магазина (merchant_id)
# ══════════════════════════════════════════════════════════════════════════════
def _stats_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(_b("Сегодня", "fm:today"), _b("7 дней", "fm:7d"))
    kb.row(_b("30 дней", "fm:30d"), _b("Этот месяц", "fm:month"))
    kb.row(_b("Всё время", "fm:all"))
    kb.row(_b("📅 Свой период", "fm_custom"))
    kb.row(_b("💰 Прибыль и вывод", "pf_home"))
    kb.row(_b("⬅️ В кабинет", "ma_home"))
    return kb


def _fmt_mstats(label: str, u: dict, s: dict, balances: Decimal) -> str:
    return (
        f"📊 <b>Статистика</b>\n"
        f"Период: <b>{label}</b>\n\n"
        f"👥 <b>Пользователи</b>\n"
        f"Всего: <b>{u['total']}</b>\n"
        f"Новых за период: <b>{u['new']}</b>\n"
        f"Сумма балансов клиентов: <b>{texts.rub(balances)}</b>\n\n"
        f"🛒 <b>Продажи</b>\n"
        f"Заказов: <b>{s['orders']}</b>\n"
        f"Товаров продано: <b>{s['items']}</b>\n"
        f"Выручка: <b>{texts.rub(s['revenue'])}</b>\n"
        f"Себестоимость (цена владельца): <b>{texts.rub(s['cost'])}</b>\n"
        f"Прибыль (ваша маржа): <b>{texts.rub(s['profit'])}</b>"
    )


async def _render_mstats(call_or_msg, db: Database, merchant_id: int,
                         start, end, label, edit=True) -> None:
    async with db.session() as session:
        u = await stats_service.users_stats(session, start, end, merchant_id)
        s = await stats_service.sales_stats(session, start, end, merchant_id)
        bal = await stats_service.total_balance(session, merchant_id)
    text = _fmt_mstats(label, u, s, bal)
    kb = _stats_kb().as_markup()
    if edit:
        try:
            await call_or_msg.edit_text(text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            pass
    await call_or_msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "fm_home")
async def cb_mstats_home(call: CallbackQuery, db: Database, config: BotConfig,
                         state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    start, end, label = stats_service.preset_range("today", config.shop_tz)
    await _render_mstats(call.message, db, merchant_id, start, end, label)
    await call.answer()


@router.callback_query(F.data.startswith("fm:"))
async def cb_mstats_preset(call: CallbackQuery, db: Database, config: BotConfig,
                           state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    start, end, label = stats_service.preset_range(call.data.split(":", 1)[1], config.shop_tz)
    await _render_mstats(call.message, db, merchant_id, start, end, label)
    await call.answer()


@router.callback_query(F.data == "fm_custom")
async def cb_mstats_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MerchAdmin.st_custom)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", "fm_home"))
    await _edit(
        call,
        "📅 Пришлите период:\n"
        "• один день — <code>05.10.2026</code>\n"
        "• диапазон — <code>01.10.2026 - 05.10.2026</code>",
        kb,
    )
    await call.answer()


@router.message(MerchAdmin.st_custom)
async def msg_mstats_custom(message: Message, db: Database, config: BotConfig,
                            state: FSMContext, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.replace("—", "-").split("-")]
    try:
        if len(parts) == 1:
            d = datetime.strptime(parts[0], "%d.%m.%Y")
            start, end = stats_service.day_bounds(d, config.shop_tz)
            label = parts[0]
        elif len(parts) == 2:
            d1 = datetime.strptime(parts[0], "%d.%m.%Y")
            d2 = datetime.strptime(parts[1], "%d.%m.%Y")
            start, _ = stats_service.day_bounds(d1, config.shop_tz)
            _, end = stats_service.day_bounds(d2, config.shop_tz)
            label = f"{parts[0]} — {parts[1]}"
        else:
            raise ValueError
    except ValueError:
        await message.answer("Формат: 05.10.2026 или 01.10.2026 - 05.10.2026")
        return
    await state.clear()
    await _render_mstats(message, db, merchant_id, start, end, label, edit=False)


# ══════════════════════════════════════════════════════════════════════════════
# Прибыль и вывод средств (франшиза)
# ══════════════════════════════════════════════════════════════════════════════
async def _show_profit(call: CallbackQuery, db: Database, merchant_id: int) -> None:
    async with db.session() as s:
        m = await s.get(Merchant, merchant_id)
        w = await payouts_service.wallets(s, merchant_id)
        pending = await payouts_service.list_requests(s, merchant_id, status="pending")
    total = Decimal(m.earnings_total_rub or 0) if m else Decimal(0)
    avail = Decimal(m.earnings_rub or 0) if m else Decimal(0)
    sbp = payouts_service.wallet_label("sbp", w) if w["sbp"] else "— не привязан"
    usdt = payouts_service.wallet_label("usdt", w) if w["usdt"] else "— не привязан"
    held = "" if not pending else f"\nВ заявках (ожидают): <b>{len(pending)}</b>"
    caption = (
        "💰 <b>Прибыль и вывод</b>\n\n"
        f"Заработано всего: <b>{texts.rub(total)}</b>\n"
        f"Доступно к выводу: <b>{texts.rub(avail)}</b>{held}\n\n"
        f"💳 СБП: {sbp}\n"
        f"🪙 USDT (BEP20): {usdt}\n\n"
        f"Минимальная сумма вывода — {int(payouts_service.MIN_WITHDRAW)} ₽."
    )
    kb = InlineKeyboardBuilder()
    kb.row(_b("💸 Вывести средства", "pf_wd"))
    kb.row(_b("🔗 Привязать СБП", "pf_bind:sbp"), _b("🔗 Привязать USDT", "pf_bind:usdt"))
    kb.row(_b("📋 Мои заявки", "pf_list"))
    kb.row(_b("⬅️ К статистике", "fm_home"))
    await _edit(call, caption, kb)


@router.callback_query(F.data == "pf_home")
async def cb_pf_home(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    await _show_profit(call, db, merchant_id)
    await call.answer()


# ── Привязка кошельков ───────────────────────────────────────────────────────
@router.callback_query(F.data == "pf_bind:sbp")
async def cb_pf_bind_sbp(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MerchAdmin.pf_sbp_phone)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "pf_home"))
    await _edit(call, "💳 Привязка СБП.\n\nПришлите номер телефона (например <code>+79001234567</code>).", kb)
    await call.answer()


@router.message(MerchAdmin.pf_sbp_phone)
async def msg_pf_sbp_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if len(phone) < 5:
        await message.answer("Введите корректный номер телефона."); return
    await state.update_data(phone=phone)
    await state.set_state(MerchAdmin.pf_sbp_bank)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "pf_home")).as_markup()
    await message.answer("Теперь пришлите название банка (например <code>Сбер</code>, <code>Т-Банк</code>).", reply_markup=kb)


@router.message(MerchAdmin.pf_sbp_bank)
async def msg_pf_sbp_bank(message: Message, db: Database, state: FSMContext,
                          merchant_id: int = HEAD_MERCHANT_ID) -> None:
    d = await state.get_data()
    bank = (message.text or "").strip()
    if not bank:
        await message.answer("Введите название банка."); return
    async with db.session() as s:
        await payouts_service.set_sbp(s, merchant_id, d.get("phone", ""), bank[:64])
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", "pf_home")).as_markup()
    await message.answer("✅ Кошелёк СБП привязан", reply_markup=kb)


@router.callback_query(F.data == "pf_bind:usdt")
async def cb_pf_bind_usdt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MerchAdmin.pf_usdt)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "pf_home"))
    await _edit(call, "🪙 Привязка USDT (сеть BEP20).\n\nПришлите адрес кошелька (0x…).", kb)
    await call.answer()


@router.message(MerchAdmin.pf_usdt)
async def msg_pf_usdt(message: Message, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    addr = (message.text or "").strip()
    if len(addr) < 10:
        await message.answer("Введите корректный адрес кошелька."); return
    async with db.session() as s:
        await payouts_service.set_usdt(s, merchant_id, addr[:128])
        await s.commit()
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", "pf_home")).as_markup()
    await message.answer("✅ Кошелёк USDT привязан", reply_markup=kb)


# ── Заявка на вывод ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "pf_wd")
async def cb_pf_withdraw(call: CallbackQuery, db: Database, state: FSMContext,
                         merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    async with db.session() as s:
        w = await payouts_service.wallets(s, merchant_id)
        avail = await payouts_service.available(s, merchant_id)
    methods = [m for m in ("sbp", "usdt") if w[m]]
    if not methods:
        kb = InlineKeyboardBuilder().row(_b("🔗 Привязать кошелёк", "pf_home"))
        await _edit(call, "Сначала привяжите кошелёк (СБП или USDT).", kb)
        await call.answer(); return
    # Сумму спрашиваем всегда (даже если доступного мало) — проверка минимума и
    # достаточности средств идёт в msg_pf_amount и возвращает ошибку с кнопкой
    # «Назад». Иначе ранний блок обрывал флоу: введённые суммы оставались без
    # ответа (состояние ввода не включалось).
    if len(methods) == 1:
        await _ask_amount(call, state, methods[0], avail)
    else:
        kb = InlineKeyboardBuilder()
        kb.row(_b("💳 На СБП", "pf_m:sbp"), _b("🪙 На USDT", "pf_m:usdt"))
        kb.row(_b("⬅️ Отмена", "pf_home"))
        await _edit(call, "Куда вывести средства?", kb)
    await call.answer()


async def _ask_amount(call: CallbackQuery, state: FSMContext, method: str, avail: Decimal) -> None:
    await state.set_state(MerchAdmin.pf_amount)
    await state.update_data(method=method)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Отмена", "pf_home"))
    await _edit(
        call,
        f"💸 Вывод на {payouts_service.METHOD_NAMES[method]}.\n\n"
        f"Доступно: <b>{texts.rub(avail)}</b>. Минимум — {int(payouts_service.MIN_WITHDRAW)} ₽.\n"
        "Пришлите сумму в рублях (например <code>500</code>).",
        kb,
    )


@router.callback_query(F.data.startswith("pf_m:"))
async def cb_pf_method(call: CallbackQuery, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
    method = call.data.split(":", 1)[1]
    async with db.session() as s:
        avail = await payouts_service.available(s, merchant_id)
    await _ask_amount(call, state, method, avail)
    await call.answer()


@router.message(MerchAdmin.pf_amount)
async def msg_pf_amount(message: Message, db: Database, config: BotConfig, state: FSMContext,
                        bots_by_merchant: dict = None, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    d = await state.get_data()
    method = d.get("method", "sbp")
    _back = InlineKeyboardBuilder().row(_b("⬅️ Назад", "pf_home")).as_markup()
    val = _num(message.text or "")
    if val is None:
        await message.answer("Введите число, например 500.", reply_markup=_back); return
    async with db.session() as s:
        payout, err = await payouts_service.create_request(s, merchant_id, val, method)
        if err:
            await s.rollback()
            await message.answer("⚠️ " + err, reply_markup=_back); return
        await s.commit()
        pid, amount, wallet = payout.id, payout.amount_rub, payout.wallet
        m = await s.get(Merchant, merchant_id)
        title = m.title if m else str(merchant_id)
    await state.clear()
    kb = InlineKeyboardBuilder().row(_b("⬅️ В кабинет", "ma_home")).as_markup()
    await message.answer(
        f"✅ Заявка №{pid} на вывод <b>{texts.rub(amount)}</b> создана.\n"
        f"Реквизиты: {wallet}\n\nОжидайте одобрения владельцем.",
        reply_markup=kb,
    )
    # Уведомить владельца платформы через головной бот.
    head_bot = (bots_by_merchant or {}).get(HEAD_MERCHANT_ID)
    if head_bot is not None and config is not None:
        for admin_id in config.admin_ids:
            try:
                await head_bot.send_message(
                    admin_id,
                    f"💸 Новая заявка на вывод №{pid} от франшизы «{title}»: "
                    f"{texts.rub(amount)} ({wallet}).\n"
                    "Админ-панель → Рефералы → Мерчанты → выберите франшизу → Выплаты.",
                )
            except Exception:  # noqa: BLE001
                pass


@router.callback_query(F.data == "pf_list")
async def cb_pf_list(call: CallbackQuery, db: Database, state: FSMContext,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    async with db.session() as s:
        reqs = await payouts_service.list_requests(s, merchant_id, limit=15)
    if not reqs:
        lines = ["📋 <b>Мои заявки</b>\n\nЗаявок пока нет."]
    else:
        lines = ["📋 <b>Мои заявки</b>\n"]
        st_ru = {"pending": "⏳ ожидает", "approved": "✅ выполнена", "rejected": "❌ отклонена"}
        for p in reqs:
            d = p.created_at.strftime("%d.%m.%Y") if p.created_at else "—"
            line = f"№{p.id} · {texts.rub(p.amount_rub)} · {p.wallet} · {d} · {st_ru.get(p.status, p.status)}"
            if p.status == "rejected" and p.reason:
                line += f"\n   причина: {p.reason}"
            lines.append(line)
    kb = InlineKeyboardBuilder().row(_b("⬅️ Назад", "pf_home"))
    await _edit(call, "\n".join(lines), kb)
    await call.answer()


# ══════════════════════════════════════════════════════════════════════════════
# Регистрация движка рассылки франшизы (prefix "mb", merchant_id > 0).
# ══════════════════════════════════════════════════════════════════════════════
class FranchiseBroadcast(StatesGroup):
    compose = State()
    url = State()
    label = State()
    emoji = State()
    schedule = State()


broadcast_flow.register(
    router,
    prefix="mb",
    home_cb="ma_home",
    title="Рассылка",
    sections=broadcast_flow.FRANCHISE_SECTIONS,
    states=FranchiseBroadcast,
)

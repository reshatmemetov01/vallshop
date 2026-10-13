"""Инлайн админ-панель (видна только ADMIN_IDS).

Управление каталогом кнопками: товары, номиналы, цена/наценка, сток, видимость,
заказы. Ввод значений — короткими сообщениями (FSM). Текстовые команды из
admin.py тоже продолжают работать как запасной вариант.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import Order, Product, StockItem, StockStatus, User, Variant
from ..services import catalog as catalog_service
from ..services import smartprice as smartprice_service
from ..services import stock as stock_service
from ..services.pricing import (
    _fmt_rub, margin, price_label, price_rub_value, round_price_rub, sale_price, usd_to_rub,
)
from ..services import rates as rates_service
from fazercard import FazerCardClient, FazerCardError
from .. import texts

log = logging.getLogger("vallshop.admin_ui")

router = Router()


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


class AdminUI(StatesGroup):
    add_product = State()
    add_variant = State()
    set_price = State()
    set_markup = State()
    add_stock = State()
    set_prod_emoji = State()
    set_var_emoji = State()
    set_prod_title = State()
    set_prod_desc = State()
    add_variant_manual = State()
    add_subcat = State()
    set_var_title = State()
    set_price_rub = State()
    set_text = State()
    set_banner = State()
    set_rate = State()
    set_src_lio = State()
    set_src_vpn = State()
    vpn_servers = State()
    steam_markup = State()
    steam_min = State()
    steam_max = State()
    stars_markup = State()
    stars_min = State()
    stars_max = State()
    fzr_search = State()
    fzr_price = State()


def _first_custom_emoji(message: Message) -> Optional[str]:
    """Вернуть custom_emoji_id первого премиум-эмодзи в сообщении, если есть."""
    entities = (message.entities or []) + (message.caption_entities or [])
    for e in entities:
        cid = getattr(e, "custom_emoji_id", None)
        if cid:
            return cid
    return None


def _emoji_id_from(message: Message) -> Optional[str]:
    """custom_emoji_id из присланного премиум-эмодзи ИЛИ из набранного числа-ID."""
    cid = _first_custom_emoji(message)
    if cid:
        return cid
    raw = (message.text or "").strip()
    return raw if raw.isdigit() and 5 <= len(raw) <= 25 else None


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ── Рендер экранов ───────────────────────────────────────────────────────────
async def _panel_caption(session) -> str:
    products = await session.scalar(select(func.count()).select_from(Product))
    variants = await session.scalar(select(func.count()).select_from(Variant))
    in_stock = await session.scalar(
        select(func.count()).select_from(StockItem).where(
            StockItem.status == StockStatus.AVAILABLE
        )
    )
    orders = await session.scalar(select(func.count()).select_from(Order))
    return (
        "🛠 <b>Админ-панель VallShop</b>\n\n"
        f"Товаров: <b>{products}</b>\n"
        f"Номиналов: <b>{variants}</b>\n"
        f"Кодов в стоке: <b>{in_stock}</b>\n"
        f"Заказов: <b>{orders}</b>"
    )


def _panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📦 Товары", "a_prods"))
    kb.row(_btn("➕ Добавить товар", "a_addprod"))
    kb.row(_btn("🧾 Заказы", "a_orders"), _btn("📥 Сток", "a_stock"))
    kb.row(_btn("🛒 Текущие заказы", "co_home"))
    kb.row(_btn("🎟 Коды активации", "ac_home"))
    kb.row(_btn("⭐ Отзывы", "arv_home"), _btn("📊 Статистика", "st_home"))
    kb.row(_btn("🎨 Оформление", "ap_home"), _btn("👥 Рефералы", "rf_home"))
    kb.row(_btn("💱 Курс USDT→₽", "a_rate"), _btn("🛡 VPN", "vpn_home"))
    kb.row(_btn("💳 Пополнение Steam", "steam_home"))
    kb.row(_btn("⭐ Телеграм Звёзды", "stars_home"))
    kb.row(_btn("⬅️ Меню", "menu"))
    return kb.as_markup()


async def _show_panel(call: CallbackQuery, db: Database, edit: bool) -> None:
    async with db.session() as session:
        caption = await _panel_caption(session)
    if edit:
        try:
            await call.message.edit_text(caption, reply_markup=_panel_kb())
            return
        except Exception:  # noqa: BLE001
            pass
    await call.message.answer(caption, reply_markup=_panel_kb())


@router.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    # Открываем панель новым сообщением (меню — это фото, его не edit_text'нуть).
    await _show_panel(call, db, edit=False)
    await call.answer()


@router.callback_query(F.data == "a_home")
async def cb_admin_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_panel(call, db, edit=True)
    await call.answer()


@router.callback_query(F.data == "a_rate")
async def cb_rate(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.set_rate)
    cur = _fmt_rub(rates_service.get_rate())
    await call.message.edit_text(
        f"💱 <b>Курс USDT→₽</b>\nСейчас: 1 USDT = <b>{cur} ₽</b>\n\n"
        "Пришлите новый курс (например <code>95</code>). "
        "По нему считаются рублёвые цены и зачисления.",
        reply_markup=_cancel_kb("a_home"),
    )
    await call.answer()


@router.message(AdminUI.set_rate)
async def msg_rate(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        rate = Decimal(raw)
        if rate <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("Введите положительное число, например 95")
        return
    await rates_service.set_rate(db, rate)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ В админ-панель", "a_home"))
    await message.answer(
        f"✅ Курс обновлён: 1 USDT = {_fmt_rub(rate)} ₽", reply_markup=kb.as_markup()
    )


# ── VPN (VPNresellers) ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "vpn_home")
async def cb_vpn_home(call: CallbackQuery, db: Database, state: FSMContext, vpn=None) -> None:
    await state.clear()
    balance = "—"
    if vpn is not None and getattr(vpn, "configured", False):
        balance = await asyncio.to_thread(vpn.balance) or "—"
    async with db.session() as session:
        from ..services import settings as settings_service
        ids = await settings_service.get(session, settings_service.VPN_SERVER_IDS, "") or "—"
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🧩 Выбрать серверы", "vpn_pick"))
    kb.row(_btn("📋 Список серверов", "vpn_list"))
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(
        "🛡 <b>VPN (VPNresellers)</b>\n"
        f"Баланс реселлера: <b>{balance}</b>\n"
        f"Серверы в подписке (id): <b>{ids}</b>\n"
        "Протокол: <b>VLESS</b>\n\n"
        "Серверы попадут в ссылку-подписку клиента (он переключает их в приложении).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data == "vpn_list")
async def cb_vpn_list(call: CallbackQuery, vpn=None) -> None:
    if vpn is None or not getattr(vpn, "configured", False):
        await call.answer("VPN не настроен (нет токена)", show_alert=True)
        return
    try:
        servers = await asyncio.to_thread(vpn.list_vless_servers)
    except Exception as e:  # noqa: BLE001
        await call.answer(f"Ошибка: {e}", show_alert=True)
        return
    lines = ["🌍 <b>VLESS-серверы</b> (id · страна · город):", ""]
    for s in servers[:60]:
        sid = s.get("id")
        cc = s.get("country_code") or s.get("country") or ""
        city = s.get("city") or ""
        st = s.get("status") or ""
        lines.append(f"<code>{sid}</code> · {cc} {city} {('· ' + str(st)) if st else ''}")
    lines.append("\nСкопируй нужные id и задай их в «✏️ Выбрать серверы».")
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Назад", "vpn_home"))
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await call.answer()


def _vpn_server_label(s: dict) -> tuple[str, str]:
    sid = str(s.get("id"))
    cc = s.get("country_code") or s.get("country") or ""
    city = s.get("city") or ""
    name = f"{cc} {city}".strip() or (s.get("name") or sid)
    return sid, name


def _vpn_pick_markup(servers: list, selected: set) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in servers[:50]:
        sid, name = _vpn_server_label(s)
        mark = "✅" if sid in selected else "⬜"
        kb.row(_btn(f"{mark} {sid} · {name}", f"vpn_tog:{sid}"))
    kb.row(_btn("✅ Выбрать все", "vpn_all"), _btn("⬜ Снять все", "vpn_none"))
    kb.row(_btn("💾 Сохранить", "vpn_save"))
    kb.row(_btn("✏️ Ввести id вручную", "vpn_manual"), _btn("⬅️ Назад", "vpn_home"))
    return kb.as_markup()


def _vpn_pick_caption(selected: set) -> str:
    chosen = ", ".join(sorted(selected, key=lambda x: (len(x), x))) or "—"
    return (
        "✏️ <b>Выбор серверов</b>\n"
        "Отметьте серверы — они войдут в подписку клиента (он переключает их в приложении).\n\n"
        f"Выбрано (<b>{len(selected)}</b>): {chosen}\n\n"
        "После «💾 Сохранить» подписки уже купивших клиентов пересоберутся "
        "автоматически (новые серверы появятся при обновлении в приложении)."
    )


@router.callback_query(F.data == "vpn_pick")
async def cb_vpn_pick(call: CallbackQuery, db: Database, state: FSMContext, vpn=None) -> None:
    if vpn is None or not getattr(vpn, "configured", False):
        # Нет токена — только ручной ввод.
        await _vpn_manual_prompt(call, state)
        return
    try:
        servers = await asyncio.to_thread(vpn.list_vless_servers)
    except Exception as e:  # noqa: BLE001
        await call.answer(f"Ошибка загрузки серверов: {e}", show_alert=True)
        return
    if not servers:
        await call.answer("Серверы не найдены — задайте id вручную", show_alert=True)
        await _vpn_manual_prompt(call, state)
        return
    async with db.session() as session:
        from ..services import settings as settings_service
        raw = await settings_service.get(session, settings_service.VPN_SERVER_IDS, "")
    selected = {p.strip() for p in (raw or "").replace(";", ",").split(",") if p.strip()}
    # В выбор оставляем только реально существующие серверы.
    available = {str(s.get("id")) for s in servers}
    selected &= available
    await state.update_data(vpn_servers=servers, vpn_sel=sorted(selected))
    await call.message.edit_text(
        _vpn_pick_caption(selected), reply_markup=_vpn_pick_markup(servers, selected)
    )
    await call.answer()


async def _vpn_rerender(call: CallbackQuery, state: FSMContext, servers, selected) -> None:
    await state.update_data(vpn_sel=sorted(selected))
    try:
        await call.message.edit_text(
            _vpn_pick_caption(selected), reply_markup=_vpn_pick_markup(servers, selected)
        )
    except Exception:  # noqa: BLE001 — «message is not modified» и т.п.
        pass


@router.callback_query(F.data.startswith("vpn_tog:"))
async def cb_vpn_toggle(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    servers = data.get("vpn_servers") or []
    selected = set(data.get("vpn_sel") or [])
    sid = call.data.split(":", 1)[1]
    selected.discard(sid) if sid in selected else selected.add(sid)
    await _vpn_rerender(call, state, servers, selected)
    await call.answer()


@router.callback_query(F.data == "vpn_all")
async def cb_vpn_all(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    servers = data.get("vpn_servers") or []
    selected = {str(s.get("id")) for s in servers}
    await _vpn_rerender(call, state, servers, selected)
    await call.answer("Выбраны все серверы")


@router.callback_query(F.data == "vpn_none")
async def cb_vpn_none(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    servers = data.get("vpn_servers") or []
    await _vpn_rerender(call, state, servers, set())
    await call.answer("Сняты все")


@router.callback_query(F.data == "vpn_save")
async def cb_vpn_save(call: CallbackQuery, db: Database, state: FSMContext, vpn=None) -> None:
    data = await state.get_data()
    selected = [str(x) for x in (data.get("vpn_sel") or [])]
    if not selected:
        await call.answer("Выберите хотя бы один сервер", show_alert=True)
        return
    await state.clear()
    from ..services import settings as settings_service
    from ..services import vpn as vpn_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.VPN_SERVER_IDS, ",".join(selected))
        await settings_service.set(session, settings_service.VPN_PROTOCOL, "vless")
        await session.commit()
    await call.message.edit_text(
        f"✅ Серверы подписки: {', '.join(selected)}\n🔄 Пересобираю подписки клиентов…"
    )
    await call.answer()
    # Пересобрать подписки уже купивших клиентов под новый набор серверов.
    updated, total = 0, 0
    try:
        async with db.session() as session:
            updated, total = await vpn_service.rebuild_active_subscriptions(
                session, vpn, server_ids=selected
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("Пересборка подписок не удалась: %s", e)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К VPN", "vpn_home"))
    tail = (
        f"\nОбновлено подписок: <b>{updated}</b> из {total}." if total
        else "\nАктивных подписок для обновления нет."
    )
    await call.message.edit_text(
        f"✅ Серверы подписки: {', '.join(selected)}.{tail}",
        reply_markup=kb.as_markup(),
    )


async def _vpn_manual_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.vpn_servers)
    await call.message.edit_text(
        "✏️ Пришлите <b>id серверов через запятую</b> (например <code>1,2,3</code>) — "
        "они войдут в подписку, которую получает клиент.",
        reply_markup=_cancel_kb("vpn_home"),
    )
    await call.answer()


@router.callback_query(F.data == "vpn_manual")
async def cb_vpn_manual(call: CallbackQuery, state: FSMContext) -> None:
    await _vpn_manual_prompt(call, state)


@router.message(AdminUI.vpn_servers)
async def msg_vpn_servers(message: Message, db: Database, state: FSMContext, vpn=None) -> None:
    raw = (message.text or "").strip()
    ids = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not ids:
        await message.answer("Пришлите id через запятую, например 1,2,3")
        return
    await state.clear()
    from ..services import settings as settings_service
    from ..services import vpn as vpn_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.VPN_SERVER_IDS, ",".join(ids))
        await settings_service.set(session, settings_service.VPN_PROTOCOL, "vless")
        await session.commit()
    updated, total = 0, 0
    try:
        async with db.session() as session:
            updated, total = await vpn_service.rebuild_active_subscriptions(
                session, vpn, server_ids=ids
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("Пересборка подписок не удалась: %s", e)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К VPN", "vpn_home"))
    tail = f" Обновлено подписок: {updated} из {total}." if total else ""
    await message.answer(
        f"✅ Серверы подписки: {', '.join(ids)}.{tail}", reply_markup=kb.as_markup()
    )


# ── Пополнение Steam (наценка, лимиты заказа) ──────────────────────────────────
@router.callback_query(F.data == "steam_home")
async def cb_steam_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    from ..services import steam as steam_service
    async with db.session() as session:
        markup = await steam_service.get_markup(session)
        lo, hi = await steam_service.get_limits(session)
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"📈 Наценка: {_fmt_rub(markup)}%", "steam_markup"))
    kb.row(
        _btn(f"⬇️ Мин: {_fmt_rub(lo)} ₽", "steam_min"),
        _btn(f"⬆️ Макс: {_fmt_rub(hi)} ₽", "steam_max"),
    )
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(
        "💳 <b>Пополнение Steam</b>\n\n"
        f"Наценка: <b>{_fmt_rub(markup)}%</b>\n"
        f"Лимит заказа (к оплате): <b>{_fmt_rub(lo)} — {_fmt_rub(hi)} ₽</b>\n\n"
        "Клиент вводит сумму на Steam в своей валюте (USD/RUB/KZT/UAH); цена в ₽ "
        "считается от себестоимости FazerCards по их курсу + наценка.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


def _parse_pos_decimal(text: Optional[str], *, allow_zero: bool = False) -> Optional[Decimal]:
    raw = (text or "").strip().replace(",", ".")
    try:
        v = Decimal(raw)
    except InvalidOperation:
        return None
    if v < 0 or (v == 0 and not allow_zero):
        return None
    return v


@router.callback_query(F.data == "steam_markup")
async def cb_steam_markup(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.steam_markup)
    await call.message.edit_text(
        "📈 Пришлите наценку в % (например <code>5</code>).",
        reply_markup=_cancel_kb("steam_home"),
    )
    await call.answer()


@router.message(AdminUI.steam_markup)
async def msg_steam_markup(message: Message, db: Database, state: FSMContext) -> None:
    val = _parse_pos_decimal(message.text, allow_zero=True)
    if val is None:
        await message.answer("Введите число ≥ 0, например 5")
        return
    from ..services import settings as settings_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.STEAM_MARKUP, str(val))
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Steam", "steam_home"))
    await message.answer(f"✅ Наценка Steam: {_fmt_rub(val)}%", reply_markup=kb.as_markup())


@router.callback_query(F.data == "steam_min")
async def cb_steam_min(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.steam_min)
    await call.message.edit_text(
        "⬇️ Пришлите <b>минимальную</b> сумму заказа к оплате, ₽ (например <code>50</code>).",
        reply_markup=_cancel_kb("steam_home"),
    )
    await call.answer()


@router.message(AdminUI.steam_min)
async def msg_steam_min(message: Message, db: Database, state: FSMContext) -> None:
    val = _parse_pos_decimal(message.text)
    if val is None:
        await message.answer("Введите положительное число, например 50")
        return
    from ..services import settings as settings_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.STEAM_MIN_RUB, str(val))
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Steam", "steam_home"))
    await message.answer(f"✅ Мин. заказ Steam: {_fmt_rub(val)} ₽", reply_markup=kb.as_markup())


@router.callback_query(F.data == "steam_max")
async def cb_steam_max(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.steam_max)
    await call.message.edit_text(
        "⬆️ Пришлите <b>максимальную</b> сумму заказа к оплате, ₽ (например <code>50000</code>).\n"
        "Удобно ограничить исходя из вашего баланса у поставщика.",
        reply_markup=_cancel_kb("steam_home"),
    )
    await call.answer()


@router.message(AdminUI.steam_max)
async def msg_steam_max(message: Message, db: Database, state: FSMContext) -> None:
    val = _parse_pos_decimal(message.text)
    if val is None:
        await message.answer("Введите положительное число, например 50000")
        return
    from ..services import settings as settings_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.STEAM_MAX_RUB, str(val))
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Steam", "steam_home"))
    await message.answer(f"✅ Макс. заказ Steam: {_fmt_rub(val)} ₽", reply_markup=kb.as_markup())


# ── Telegram Звёзды (наценка, лимиты количества) ───────────────────────────────
@router.callback_query(F.data == "stars_home")
async def cb_stars_home(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    from ..services import stars as stars_service
    from ..services import settings as settings_service
    async with db.session() as session:
        markup = await stars_service.get_markup(session)
        lo_raw = await settings_service.get(session, settings_service.STARS_MIN_QTY)
        hi_raw = await settings_service.get(session, settings_service.STARS_MAX_QTY)
    lo_lbl = f"{lo_raw} ⭐️" if lo_raw else "авто"
    hi_lbl = f"{hi_raw} ⭐️" if hi_raw else "авто"
    kb = InlineKeyboardBuilder()
    kb.row(_btn(f"📈 Наценка: {_fmt_rub(markup)}%", "stars_markup"))
    kb.row(
        _btn(f"⬇️ Мин: {lo_lbl}", "stars_min"),
        _btn(f"⬆️ Макс: {hi_lbl}", "stars_max"),
    )
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(
        "⭐️ <b>Telegram Звёзды</b>\n\n"
        f"Наценка: <b>{_fmt_rub(markup)}%</b>\n"
        f"Лимит количества: <b>{lo_lbl} — {hi_lbl}</b>\n\n"
        "Клиент сам вводит количество звёзд; цена в ₽ считается от себестоимости "
        "FazerCards (живая котировка) + наценка. «авто» — брать лимиты из "
        "котировки поставщика (50–10000).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


def _parse_pos_int(text: Optional[str]) -> Optional[int]:
    raw = (text or "").strip().replace(" ", "")
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


@router.callback_query(F.data == "stars_markup")
async def cb_stars_markup(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.stars_markup)
    await call.message.edit_text(
        "📈 Пришлите наценку в % (например <code>10</code>).",
        reply_markup=_cancel_kb("stars_home"),
    )
    await call.answer()


@router.message(AdminUI.stars_markup)
async def msg_stars_markup(message: Message, db: Database, state: FSMContext) -> None:
    val = _parse_pos_decimal(message.text, allow_zero=True)
    if val is None:
        await message.answer("Введите число ≥ 0, например 10")
        return
    from ..services import settings as settings_service
    async with db.session() as session:
        await settings_service.set(session, settings_service.STARS_MARKUP, str(val))
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Звёздам", "stars_home"))
    await message.answer(f"✅ Наценка звёзд: {_fmt_rub(val)}%", reply_markup=kb.as_markup())


@router.callback_query(F.data == "stars_min")
async def cb_stars_min(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.stars_min)
    await call.message.edit_text(
        "⬇️ Пришлите <b>минимальное</b> количество звёзд (например <code>50</code>).\n"
        "Отправьте <code>0</code>, чтобы брать лимит из котировки поставщика.",
        reply_markup=_cancel_kb("stars_home"),
    )
    await call.answer()


@router.message(AdminUI.stars_min)
async def msg_stars_min(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    from ..services import settings as settings_service
    if raw == "0":
        value, label = "", "авто"
    else:
        v = _parse_pos_int(raw)
        if v is None:
            await message.answer("Введите положительное целое число, например 50 (или 0 — авто)")
            return
        value, label = str(v), f"{v} ⭐️"
    async with db.session() as session:
        await settings_service.set(session, settings_service.STARS_MIN_QTY, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Звёздам", "stars_home"))
    await message.answer(f"✅ Мин. количество звёзд: {label}", reply_markup=kb.as_markup())


@router.callback_query(F.data == "stars_max")
async def cb_stars_max(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.stars_max)
    await call.message.edit_text(
        "⬆️ Пришлите <b>максимальное</b> количество звёзд (например <code>10000</code>).\n"
        "Отправьте <code>0</code>, чтобы брать лимит из котировки поставщика.",
        reply_markup=_cancel_kb("stars_home"),
    )
    await call.answer()


@router.message(AdminUI.stars_max)
async def msg_stars_max(message: Message, db: Database, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    from ..services import settings as settings_service
    if raw == "0":
        value, label = "", "авто"
    else:
        v = _parse_pos_int(raw)
        if v is None:
            await message.answer("Введите положительное целое число, например 10000 (или 0 — авто)")
            return
        value, label = str(v), f"{v} ⭐️"
    async with db.session() as session:
        await settings_service.set(session, settings_service.STARS_MAX_QTY, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К Звёздам", "stars_home"))
    await message.answer(f"✅ Макс. количество звёзд: {label}", reply_markup=kb.as_markup())


# ── Товары ───────────────────────────────────────────────────────────────────
async def _products_kb(session) -> InlineKeyboardMarkup:
    products = await catalog_service.list_products(session, only_active=False)
    kb = InlineKeyboardBuilder()
    for p in products:
        flag = "🟢" if p.is_active else "🔴"
        kb.row(_btn(f"{flag} {p.title}", f"a_prod:{p.id}"))
    kb.row(_btn("➕ Добавить категорию / товар", "a_addprod"))
    kb.row(_btn("⬅️ Назад", "a_home"))
    return kb.as_markup()


@router.callback_query(F.data == "a_prods")
async def cb_products(call: CallbackQuery, db: Database) -> None:
    async with db.session() as session:
        markup = await _products_kb(session)
    await call.message.edit_text("📦 <b>Товары</b>\nВыберите товар:", reply_markup=markup)
    await call.answer()


async def _product_card(session, config: BotConfig, product_id: int):
    product = await session.get(Product, product_id)
    if not product:
        return None, None
    children = await catalog_service.list_children(session, product_id, only_active=False)
    variants = await catalog_service.list_variants(session, product_id, only_active=False)
    counts = await stock_service.counts_by_variant(session, [v.id for v in variants])
    flag = "🟢 активен" if product.is_active else "🔴 выключен"
    desc = product.description or "— (нет описания)"
    kind = "📁 категория" if children else "📦 товар"
    banner = "🖼 есть" if product.banner_file_id else "— нет"
    fr = "🔴 скрыт" if getattr(product, "head_only", False) else "🟢 показан"
    caption = (
        f"{kind}: <b>{product.title}</b>\n"
        f"Игра: {product.game} · {flag}\n"
        f"Описание: {desc}\n"
        f"Баннер: {banner}\n"
        f"У франшиз: {fr}\n"
        f"Подкатегорий: {len(children)} · Номиналов: {len(variants)}"
    )
    kb = InlineKeyboardBuilder()
    # Подкатегории (навигация вглубь).
    for c in children:
        cflag = "🟢" if c.is_active else "🔴"
        kb.row(_btn(f"{cflag} 📁 {c.title}", f"a_prod:{c.id}"))
    for v in variants:
        vflag = "🟢" if v.is_active else "🔴"
        kb.row(
            _btn(
                f"{vflag} {v.title} · {price_label(v, config.default_markup_percent)} · сток {counts.get(v.id, 0)}",
                f"a_var:{v.id}",
            )
        )
    kb.row(
        _btn("✏️ Название", f"a_ptitle:{product_id}"),
        _btn("📝 Описание", f"a_pdesc:{product_id}"),
    )
    kb.row(_btn("➕ Подкатегория", f"a_addsub:{product_id}"))
    kb.row(
        _btn("➕ Номинал (LioGames)", f"a_addvar:{product_id}"),
        _btn("➕ Свой номинал", f"a_addvarm:{product_id}"),
    )
    kb.row(_btn("➕ Номинал FazerCard", f"a_addvarf:{product_id}"))
    kb.row(
        _btn("🙂 Эмодзи", f"a_pemoji:{product_id}"),
        _btn("🖼 Баннер", f"a_pbanner:{product_id}"),
    )
    kb.row(_btn("🔁 Вкл/выкл товар", f"a_tprod:{product_id}"))
    _hd = "🏪 Скрыть у франшиз" if not getattr(product, "head_only", False) else "🏪 Показать у франшиз"
    kb.row(_btn(_hd, f"a_phead:{product_id}"))
    _fw = "↔️ Во всю строку: ✅" if getattr(product, "full_width", False) else "↔️ Во всю строку: ❌"
    _cur_style = getattr(product, "button_style", None) or "primary"
    kb.row(
        _btn(f"🎨 Цвет: {_cur_style}", f"a_pstyle:{product_id}"),
        _btn(_fw, f"a_pfw:{product_id}"),
    )
    kb.row(
        _btn("⏳ Текст ожидания", f"a_txt:p:pend:{product_id}"),
        _btn("✅ Текст выдачи", f"a_txt:p:deliv:{product_id}"),
    )
    kb.row(
        _btn("📂 Переместить", f"a_pmove:{product_id}"),
        _btn("🗑 Удалить", f"a_pdel:{product_id}"),
    )
    back = f"a_prod:{product.parent_id}" if product.parent_id else "a_prods"
    kb.row(_btn("⬅️ Назад", back))
    return caption, kb.as_markup()


@router.callback_query(F.data.startswith("a_prod:"))
async def cb_product(
    call: CallbackQuery, db: Database, config: BotConfig, state: FSMContext
) -> None:
    await state.clear()  # сбросить любой незавершённый ввод (в т.ч. отмену браузера)
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        caption, markup = await _product_card(session, config, pid)
    if not caption:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("a_tprod:"))
async def cb_toggle_product(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            product.is_active = not product.is_active
            await session.commit()
        caption, markup = await _product_card(session, config, pid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Готово")


_STYLE_CYCLE = ["primary", "success", "danger"]


@router.callback_query(F.data.startswith("a_pstyle:"))
async def cb_prod_style(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            cur = product.button_style or "primary"
            nxt = _STYLE_CYCLE[(_STYLE_CYCLE.index(cur) + 1) % len(_STYLE_CYCLE)] \
                if cur in _STYLE_CYCLE else "primary"
            product.button_style = nxt
            await session.commit()
        caption, markup = await _product_card(session, config, pid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Цвет: синий/зелёный/красный")


@router.callback_query(F.data.startswith("a_phead:"))
async def cb_prod_head_only(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    """Переключить видимость товара у франшиз (head_only)."""
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            product.head_only = not bool(getattr(product, "head_only", False))
            await session.commit()
        caption, markup = await _product_card(session, config, pid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Скрыт у франшиз" if product and product.head_only else "Показан у франшиз")


@router.callback_query(F.data.startswith("a_pfw:"))
async def cb_prod_fullwidth(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if product:
            product.full_width = not bool(product.full_width)
            await session.commit()
        caption, markup = await _product_card(session, config, pid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Готово")


# ── Номинал (вариация) ───────────────────────────────────────────────────────
async def _variant_card(session, config: BotConfig, variant_id: int):
    v = await session.get(Variant, variant_id)
    if not v:
        return None, None, None
    in_stock = await stock_service.available_count(session, variant_id)
    price = sale_price(v, config.default_markup_percent)  # USDT (внутр.)
    m = margin(v, config.default_markup_percent)
    vflag = "🟢 активен" if v.is_active else "🔴 выключен"
    rate = rates_service.get_rate()
    cost_usd = Decimal(v.cost_usd or 0)
    client_rub = price_rub_value(v, config.default_markup_percent)
    # Режим цены: умная / фикс ₽ / плавает по курсу / по наценке.
    if getattr(v, "price_mode", None) == smartprice_service.MODE_SMART:
        mk = v.markup_percent if v.markup_percent is not None else 0
        mode = f"💰 умная · наценка {_fmt_rub(mk)}% · плавает от закупа"
    elif v.price_rub is not None:
        mode = "📌 фиксированная ₽ (не плавает)"
    elif v.price_usd is not None:
        mode = f"плавает по курсу ({texts.money(price)})"
    else:
        mk = v.markup_percent if v.markup_percent is not None else config.default_markup_percent
        mode = f"наценка {mk}% ({texts.money(price)}) · плавает"
    caption = (
        f"🧩 <b>{v.title}</b> ({vflag})\n"
        f"Курс: 1 USDT = {_fmt_rub(rate)} ₽\n"
        f"Закуп: {texts.money(cost_usd, config.currency)} "
        f"(≈ {texts.rub(usd_to_rub(cost_usd, rate))})\n"
        f"💰 Цена клиенту: <b>{texts.rub(client_rub)}</b>\n"
        f"Режим цены: {mode}\n"
        f"Маржа: {texts.money(m, config.currency)} (≈ {texts.rub(usd_to_rub(m, rate))})\n"
        f"Сток: <b>{in_stock}</b>\n"
        + (
            f"Тип: FazerCard · {v.fzr_kind} "
            f"(a={v.fzr_a}, b={v.fzr_b}) · под заказ"
            if v.source == "fazercard"
            else f"Тип: 🛡 VPNresellers · {v.fzr_a} дн. · {v.fzr_b or 'vless'}"
            if v.source == "vpnresellers"
            else "Тип: свой товар (без LioGames)"
            if not v.liog_product_id
            else f"LioGames: product {v.liog_product_id} / variation {v.liog_variation_id}"
        )
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✏️ Название номинала", f"a_vtitle:{variant_id}"))
    kb.row(
        _btn("💰 Цена (₽)", f"a_setprice:{variant_id}"),
        _btn("📌 Фикс ₽", f"a_setrub:{variant_id}"),
    )
    kb.row(
        _btn("📈 Наценка", f"a_setmarkup:{variant_id}"),
        _btn("📥 Добавить сток", f"a_astock:{variant_id}"),
    )
    kb.row(
        _btn("🙂 Эмодзи", f"a_vemoji:{variant_id}"),
        _btn("🔁 Вкл/выкл", f"a_tvar:{variant_id}"),
    )
    kb.row(
        _btn("⏳ Текст ожидания", f"a_txt:v:pend:{variant_id}"),
        _btn("✅ Текст выдачи", f"a_txt:v:deliv:{variant_id}"),
    )
    kb.row(_btn("🏷 Поставщик", f"a_src:{variant_id}"))
    kb.row(_btn("🗑 Удалить номинал", f"a_vdel:{variant_id}"))
    kb.row(_btn("⬅️ Назад", f"a_prod:{v.product_id}"))
    return caption, kb.as_markup(), v.product_id


@router.callback_query(F.data.startswith("a_var:"))
async def cb_variant(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        caption, markup, _ = await _variant_card(session, config, vid)
    if not caption:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("a_tvar:"))
async def cb_toggle_variant(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.is_active = not v.is_active
            await session.commit()
        caption, markup, _ = await _variant_card(session, config, vid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Готово")


# ── Удаление товаров/категорий и номиналов ─────────────────────────────────────
@router.callback_query(F.data.startswith("a_pdel:"))
async def cb_prod_delete_ask(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if not product:
            await call.answer("Не найдено", show_alert=True)
            return
        kids = await catalog_service.children_count(session, pid)
        variants = await catalog_service.list_variants(session, pid, only_active=False)
    warn = ""
    if kids or variants:
        warn = (
            f"\n\n⚠️ Внутри: подкатегорий — {kids}, номиналов — {len(variants)}. "
            "Будет удалено всё вместе."
        )
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить", f"a_pdelok:{pid}"))
    kb.row(_btn("🗑 Удалить с заказами", f"a_pdelf:{pid}"))
    kb.row(_btn("⬅️ Отмена", f"a_prod:{pid}"))
    await call.message.edit_text(
        f"Удалить <b>{product.title}</b>?{warn}\n\n"
        "Удаление необратимо. Если по номиналам уже были заказы — обычное "
        "удаление нельзя (используйте «🔁 Вкл/выкл» или «🗑 Удалить с заказами»).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_pdelf:"))
async def cb_prod_delete_force_ask(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if not product:
            await call.answer("Не найдено", show_alert=True)
            return
        orders = await catalog_service.product_order_count(session, pid)
        title = product.title
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить всё (с заказами)", f"a_pdelfok:{pid}"))
    kb.row(_btn("⬅️ Отмена", f"a_prod:{pid}"))
    await call.message.edit_text(
        f"⚠️ Удалить <b>{title}</b> ВМЕСТЕ с заказами?\n\n"
        f"Будет удалено заказов: <b>{orders}</b> (и связанные платежи, отзывы, "
        "реферальные начисления).\n\n"
        "❗️ Это <b>необратимо</b> и задним числом изменит статистику: выручка и "
        "прибыль по этим заказам пропадут, у клиентов исчезнет история покупок.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_pdelfok:"))
async def cb_prod_delete_force(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        parent_id = product.parent_id if product else None
        ok, reason = await catalog_service.force_delete_product(session, pid)
    if not ok:
        await call.answer("Не найдено", show_alert=True)
        return
    if parent_id:
        async with db.session() as session:
            caption, markup = await _product_card(session, config, parent_id)
        await call.message.edit_text(caption, reply_markup=markup)
    else:
        async with db.session() as session:
            markup = await _products_kb(session)
        await call.message.edit_text(
            "📦 <b>Товары</b>\nВыберите товар:", reply_markup=markup
        )
    await call.answer("Удалено вместе с заказами")


@router.callback_query(F.data.startswith("a_pdelok:"))
async def cb_prod_delete(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        parent_id = product.parent_id if product else None
        ok, reason = await catalog_service.delete_product(session, pid)
    if not ok and reason == "orders":
        await call.answer(
            "Нельзя удалить: по номиналам есть заказы. Отключите кнопкой «Вкл/выкл».",
            show_alert=True,
        )
        return
    if not ok:
        await call.answer("Не найдено", show_alert=True)
        return
    # Вернуться к родителю или в список товаров.
    if parent_id:
        async with db.session() as session:
            caption, markup = await _product_card(session, config, parent_id)
        await call.message.edit_text(caption, reply_markup=markup)
    else:
        async with db.session() as session:
            markup = await _products_kb(session)
        await call.message.edit_text(
            "📦 <b>Товары</b>\nВыберите товар:", reply_markup=markup
        )
    await call.answer("Удалено")


@router.callback_query(F.data.startswith("a_vdel:"))
async def cb_var_delete_ask(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await call.answer("Не найдено", show_alert=True)
            return
        title = v.title
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить", f"a_vdelok:{vid}"))
    kb.row(_btn("🗑 Удалить с заказами", f"a_vdelf:{vid}"))
    kb.row(_btn("⬅️ Отмена", f"a_var:{vid}"))
    await call.message.edit_text(
        f"Удалить номинал <b>{title}</b>?\n\n"
        "Удаление необратимо. Если по нему уже были заказы — обычное удаление "
        "нельзя (используйте «🔁 Вкл/выкл» или «🗑 Удалить с заказами»).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_vdelf:"))
async def cb_var_delete_force_ask(call: CallbackQuery, db: Database) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await call.answer("Не найдено", show_alert=True)
            return
        title = v.title
        orders = await catalog_service.variant_order_count(session, vid)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить всё (с заказами)", f"a_vdelfok:{vid}"))
    kb.row(_btn("⬅️ Отмена", f"a_var:{vid}"))
    await call.message.edit_text(
        f"⚠️ Удалить номинал <b>{title}</b> ВМЕСТЕ с заказами?\n\n"
        f"Будет удалено заказов: <b>{orders}</b> (и связанные платежи, отзывы, "
        "реферальные начисления).\n\n"
        "❗️ Это <b>необратимо</b> и задним числом изменит статистику: выручка и "
        "прибыль по этим заказам пропадут, у клиентов исчезнет история покупок.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_vdelfok:"))
async def cb_var_delete_force(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        product_id = v.product_id if v else None
        ok, reason = await catalog_service.force_delete_variant(session, vid)
    if not ok:
        await call.answer("Не найдено", show_alert=True)
        return
    async with db.session() as session:
        caption, markup = await _product_card(session, config, product_id)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Удалено вместе с заказами")


@router.callback_query(F.data.startswith("a_vdelok:"))
async def cb_var_delete(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        product_id = v.product_id if v else None
        ok, reason = await catalog_service.delete_variant(session, vid)
    if not ok and reason == "orders":
        await call.answer(
            "Нельзя удалить: по номиналу есть заказы. Отключите кнопкой «Вкл/выкл».",
            show_alert=True,
        )
        return
    if not ok:
        await call.answer("Не найдено", show_alert=True)
        return
    async with db.session() as session:
        caption, markup = await _product_card(session, config, product_id)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Удалено")


# ── Перемещение товара/категории ───────────────────────────────────────────────
@router.callback_query(F.data.startswith("a_pmove:"))
async def cb_prod_move_ask(call: CallbackQuery, db: Database) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        product = await session.get(Product, pid)
        if not product:
            await call.answer("Не найдено", show_alert=True)
            return
        candidates = await catalog_service.move_candidates(session, pid)
        cur_parent = product.parent_id
    kb = InlineKeyboardBuilder()
    if cur_parent is not None:
        kb.row(_btn("⬆️ На верхний уровень", f"a_pmv:{pid}:0"))
    for cand in candidates:
        if cand.id == cur_parent:
            continue  # уже там
        prefix = "📁 " if cand.parent_id is None else "└ "
        kb.row(_btn(f"{prefix}{cand.title}", f"a_pmv:{pid}:{cand.id}"))
    kb.row(_btn("⬅️ Отмена", f"a_prod:{pid}"))
    await call.message.edit_text(
        f"📂 Куда переместить <b>{product.title}</b>?\n"
        "Выберите категорию-контейнер (или вынесите на верхний уровень).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_pmv:"))
async def cb_prod_move(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    _, raw_pid, raw_dest = call.data.split(":", 2)
    pid = int(raw_pid)
    dest = int(raw_dest)
    new_parent = None if dest == 0 else dest
    async with db.session() as session:
        ok, reason = await catalog_service.reparent_product(session, pid, new_parent)
    if not ok:
        msg = {
            "cycle": "Нельзя переместить категорию в свою же подкатегорию.",
            "self": "Нельзя переместить в саму себя.",
            "missing": "Не найдено.",
        }.get(reason, "Не удалось переместить.")
        await call.answer(msg, show_alert=True)
        return
    async with db.session() as session:
        caption, markup = await _product_card(session, config, pid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Перемещено")


# ── FSM: ввод значений ───────────────────────────────────────────────────────
def _cancel_kb(back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Отмена", back))
    return kb.as_markup()


@router.callback_query(F.data == "a_addprod")
async def cb_add_product(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.add_product)
    await call.message.edit_text(
        "➕ <b>Новая категория / товар</b>\n\n"
        "Пришлите <b>название</b> (например <code>Прямое пополнение</code> или "
        "<code>Подарочные карты</code>).\n"
        "Можно с описанием: <code>Название | описание</code>.\n\n"
        "Внутрь потом добавите подкатегории или номиналы.",
        reply_markup=_cancel_kb("a_prods"),
    )
    await call.answer()


@router.message(AdminUI.add_product)
async def msg_add_product(message: Message, db: Database, state: FSMContext) -> None:
    parts = [p.strip() for p in (message.html_text or message.text or "").split("|")]
    title = parts[0] if parts else ""
    if not title:
        await message.answer("Название пустое. Пришлите текст.")
        return
    desc = parts[1] if len(parts) > 1 and parts[1] else None
    async with db.session() as session:
        # game — внутреннее поле (покупателю не показывается); для категории
        # дублируем название, чтобы не требовать его отдельно.
        p = Product(game=title[:64], title=title[:255], description=desc)
        session.add(p)
        await session.commit()
        pid = p.id
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("➕ Подкатегория", f"a_addsub:{pid}"))
    kb.row(
        _btn("➕ Номинал (LioGames)", f"a_addvar:{pid}"),
        _btn("➕ Номинал FazerCard", f"a_addvarf:{pid}"),
    )
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer(f"✅ Создано: <b>{title}</b>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_addvar:"))
async def cb_add_variant(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.add_variant)
    await state.update_data(pid=pid)
    await call.message.edit_text(
        "➕ <b>Новый номинал</b>\n\nПришлите: "
        "<code>Название | liog_product_id | liog_variation_id | закуп_usd [| цена_usd]</code>\n"
        "Пример: <code>325 UC | 66599 | 534125 | 4.44</code>",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.add_variant)
async def msg_add_variant(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 4:
        await message.answer("Формат: Название | liog_product_id | liog_variation_id | закуп_usd [| цена_usd]")
        return
    try:
        liog_pid = int(parts[1]); liog_vid = int(parts[2]); cost = Decimal(parts[3])
    except (ValueError, InvalidOperation):
        await message.answer("liog_product_id/liog_variation_id — числа, закуп — число (4.44)")
        return
    price = None
    if len(parts) > 4 and parts[4] not in ("", "-"):
        try:
            price = Decimal(parts[4])
        except InvalidOperation:
            await message.answer("цена_usd — число или - ")
            return
    async with db.session() as session:
        v = Variant(
            product_id=pid, title=parts[0],
            liog_product_id=liog_pid, liog_variation_id=liog_vid,
            cost_usd=cost, price_usd=price,
        )
        session.add(v)
        await session.commit()
        vid = v.id
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📥 Добавить сток", f"a_astock:{vid}"))
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer(f"✅ Номинал создан: <b>{parts[0]}</b>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_addsub:"))
async def cb_add_subcat(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.add_subcat)
    await state.update_data(parent_id=pid)
    await call.message.edit_text(
        "➕ <b>Новая подкатегория</b>\n\nПришлите её название "
        "(например <code>PUBG UC Global</code> или <code>PUBG UC RU</code>).\n"
        "Можно с описанием: <code>Название | описание</code>.",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.add_subcat)
async def msg_add_subcat(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    parent_id = data.get("parent_id")
    parts = [p.strip() for p in (message.text or "").split("|")]
    title = parts[0] if parts else ""
    if not title:
        await message.answer("Название пустое. Пришлите текст.")
        return
    desc = parts[1] if len(parts) > 1 and parts[1] else None
    async with db.session() as session:
        parent = await session.get(Product, parent_id)
        if not parent:
            await state.clear()
            await message.answer("Родительский товар не найден.")
            return
        child = Product(
            game=parent.game, title=title, description=desc,
            parent_id=parent_id,
        )
        session.add(child)
        await session.commit()
        cid = child.id
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("➕ Номинал (LioGames)", f"a_addvar:{cid}"))
    kb.row(_btn("➕ Номинал FazerCard", f"a_addvarf:{cid}"))
    kb.row(_btn("⬅️ К подкатегории", f"a_prod:{cid}"))
    kb.row(_btn("⬅️ К родителю", f"a_prod:{parent_id}"))
    await message.answer(
        f"✅ Подкатегория создана: <b>{title}</b>\nДобавьте в неё номиналы.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("a_ptitle:"))
async def cb_prod_title(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_prod_title)
    await state.update_data(pid=pid)
    await call.message.edit_text(
        "✏️ Пришлите новое <b>название</b> товара.",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.set_prod_title)
async def msg_prod_title(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название пустое. Пришлите текст.")
        return
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.title = title
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer("✅ Название обновлено", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_pdesc:"))
async def cb_prod_desc(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        p = await session.get(Product, pid)
        cur = p.description if p else None
    if cur:
        cur_line = f"сейчас:\n<blockquote>{cur}</blockquote>\n\n"
    else:
        cur_line = "сейчас: <i>нет описания</i>\n\n"
    await state.set_state(AdminUI.set_prod_desc)
    await state.update_data(pid=pid)
    await call.message.edit_text(
        "📝 Редактирование <b>описания</b> (покажется покупателю при клике).\n"
        f"{cur_line}"
        "Пришлите новый текст. Можно использовать премиум-эмодзи и "
        "форматирование (жирный, курсив) — они сохранятся.\n"
        "<code>-</code> — убрать описание.",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.set_prod_desc)
async def msg_prod_desc(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    # html_text сохраняет премиум-эмодзи (custom_emoji) и форматирование как HTML.
    raw = (message.html_text or message.text or "").strip()
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.description = None if raw == "-" else raw
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer("✅ Описание обновлено", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_pbanner:"))
async def cb_prod_banner(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        p = await session.get(Product, pid)
        has = bool(p and p.banner_file_id)
    cur = "сейчас: 🖼 установлен\n\n" if has else "сейчас: — нет\n\n"
    await state.set_state(AdminUI.set_banner)
    await state.update_data(pid=pid)
    await call.message.edit_text(
        "🖼 <b>Баннер категории</b> (картинка над списком при входе в категорию).\n"
        f"{cur}"
        "Пришлите <b>фото</b> — оно станет баннером этой категории.\n"
        "<code>-</code> — убрать (вернуть общий баннер «Каталог»).",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.set_banner, F.photo)
async def msg_prod_banner_photo(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    file_id = message.photo[-1].file_id  # самое большое доступное разрешение
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.banner_file_id = file_id
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer("✅ Баннер категории обновлён", reply_markup=kb.as_markup())


@router.message(AdminUI.set_banner)
async def msg_prod_banner_text(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    raw = (message.text or "").strip()
    if raw != "-":
        await message.answer("Пришлите фото или <code>-</code>, чтобы убрать баннер.")
        return
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.banner_file_id = None
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer("✅ Баннер убран (общий «Каталог»)", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_addvarm:"))
async def cb_add_variant_manual(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.add_variant_manual)
    await state.update_data(pid=pid)
    await call.message.edit_text(
        "➕ <b>Свой номинал</b> (без LioGames, выдача из стока)\n\n"
        "Пришлите: <code>Название | закуп_usd [| цена_usd]</code>\n"
        "Пример: <code>Аккаунт Netflix | 2.00 | 5.00</code>",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.add_variant_manual)
async def msg_add_variant_manual(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 2:
        await message.answer("Формат: Название | закуп_usd [| цена_usd]")
        return
    try:
        cost = Decimal(parts[1])
    except InvalidOperation:
        await message.answer("закуп_usd — число (например 2.00)")
        return
    price = None
    if len(parts) > 2 and parts[2] not in ("", "-"):
        try:
            price = Decimal(parts[2])
        except InvalidOperation:
            await message.answer("цена_usd — число или -")
            return
    async with db.session() as session:
        # Уникальный отрицательный liog_variation_id (своё, не из LioGames).
        min_vid = await session.scalar(
            select(func.min(Variant.liog_variation_id)).where(Variant.product_id == pid)
        )
        new_vid = min(0, int(min_vid or 0)) - 1
        v = Variant(
            product_id=pid, title=parts[0],
            liog_product_id=0, liog_variation_id=new_vid,
            cost_usd=cost, price_usd=price,
        )
        session.add(v)
        await session.commit()
        vid = v.id
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📥 Добавить сток", f"a_astock:{vid}"))
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer(
        f"✅ Свой номинал создан: <b>{parts[0]}</b>\nДобавьте коды в сток.",
        reply_markup=kb.as_markup(),
    )


# ── Номинал FazerCard (buy-on-demand): браузер каталога ────────────────────────
_FZR_KINDS = {
    "gamekey": "ключ игры",
    "giftcard": "подарочная карта",
    "topup": "пополнение игры",
}
_FZR_RESULTS = 20   # сколько категорий показывать по поиску
_FZR_OFFERS = 40    # сколько номиналов показывать в категории


# ── Поставщик номинала (источник выдачи) ────────────────────────────────────
_SOURCE_RU = {
    "stock": "📦 свой сток",
    "liogames": "🎮 LioGames",
    "fazercard": "🧩 FazerCard",
    "vpnresellers": "🛡 VPNresellers (VPN)",
}


@router.callback_query(F.data.startswith("a_src:"))
async def cb_source(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await call.answer("Не найдено", show_alert=True)
            return
        cur = _SOURCE_RU.get(v.source, v.source or "—")
        detail = ""
        if v.source == "fazercard":
            detail = f"\n{v.fzr_kind} · a={v.fzr_a} · b={v.fzr_b}"
        elif v.source == "liogames":
            detail = f"\nproduct {v.liog_product_id} / variation {v.liog_variation_id}"
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📦 Свой сток", f"a_srcset:{vid}:stock"))
    kb.row(_btn("🧩 FazerCard", f"a_srcfzr:{vid}"))
    kb.row(_btn("🛡 VPNresellers (VPN)", f"a_srcvpn:{vid}"))
    kb.row(_btn("🎮 LioGames", f"a_srclio:{vid}"))
    kb.row(_btn("⬅️ Назад", f"a_var:{vid}"))
    await call.message.edit_text(
        f"🏷 <b>Поставщик номинала</b>\nСейчас: <b>{cur}</b>{detail}\n\n"
        "Выберите, откуда выдавать этот номинал. Так можно переключить источник, "
        "не пересоздавая номинал (например, кончился свой сток — ставим FazerCard).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_srcset:"))
async def cb_source_set(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    _, raw_vid, src = call.data.split(":", 2)
    vid = int(raw_vid)
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.source = src  # пока используется только "stock"
            await session.commit()
        caption, markup, _ = await _variant_card(session, config, vid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Источник: свой сток")


@router.callback_query(F.data.startswith("a_srcfzr:"))
async def cb_source_fzr(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    """Привязать номинал к FazerCard (редактирование существующего)."""
    vid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await call.answer("Не найдено", show_alert=True)
            return
        pid = v.product_id
    # edit_vid в state переводит FazerCard-флоу в режим редактирования.
    await state.update_data(pid=pid, edit_vid=vid)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🔑 Ключ игры", "a_fzrk:gamekey"))
    kb.row(_btn("🎁 Подарочная карта", "a_fzrk:giftcard"))
    kb.row(_btn("💠 Пополнение игры", "a_fzrk:topup"))
    kb.row(_btn("⬅️ Назад", f"a_src:{vid}"))
    await call.message.edit_text(
        "🧩 <b>FazerCard</b> — привязка к номиналу\n\nВыберите тип:",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_srclio:"))
async def cb_source_lio(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_src_lio)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "🎮 <b>LioGames</b> — привязка к номиналу\n\n"
        "Пришлите: <code>product_id | variation_id</code>\n"
        "Пример: <code>66599 | 534124</code>\n"
        "Выдача пойдёт из вашего стока кодов (как source=liogames).",
        reply_markup=_cancel_kb(f"a_src:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_src_lio)
async def msg_source_lio(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    parts = [p.strip() for p in (message.text or "").split("|")]
    if len(parts) < 2:
        await message.answer("Формат: product_id | variation_id")
        return
    try:
        lp, lv = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("product_id и variation_id — числа")
        return
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.source = "liogames"
            v.liog_product_id = lp
            v.liog_variation_id = lv
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer("✅ Источник: LioGames", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_srcvpn:"))
async def cb_source_vpn(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_src_vpn)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "🛡 <b>VPNresellers (VPN)</b> — привязка к номиналу\n\n"
        "Пришлите <b>срок подписки в днях</b> (например <code>30</code>, "
        "<code>90</code>, <code>365</code>).\n"
        "Серверы и протокол берутся из «Админ-панель → 🛡 VPN».",
        reply_markup=_cancel_kb(f"a_src:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_src_vpn)
async def msg_source_vpn(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    try:
        days = int((message.text or "").strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Срок — положительное число дней, например 30")
        return
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.source = "vpnresellers"
            v.fzr_kind = "vpn"
            v.fzr_a = str(days)   # срок подписки (дней)
            v.fzr_b = "vless"     # протокол
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer(
        f"✅ Источник: VPNresellers · срок {days} дн.", reply_markup=kb.as_markup()
    )


@router.callback_query(F.data.startswith("a_addvarf:"))
async def cb_add_variant_fzr(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.update_data(pid=pid, edit_vid=None)  # создание нового номинала
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🔑 Ключ игры", "a_fzrk:gamekey"))
    kb.row(_btn("🎁 Подарочная карта", "a_fzrk:giftcard"))
    kb.row(_btn("💠 Пополнение игры", "a_fzrk:topup"))
    kb.row(_btn("⬅️ Назад", f"a_prod:{pid}"))
    await call.message.edit_text(
        "➕ <b>Номинал FazerCard</b> (под заказ)\n\nВыберите тип:",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a_fzrk:"))
async def cb_fzr_kind(call: CallbackQuery, state: FSMContext) -> None:
    kind = call.data.split(":", 1)[1]
    if kind not in _FZR_KINDS:
        await call.answer("Неизвестный тип", show_alert=True)
        return
    data = await state.get_data()
    pid = data.get("pid")
    await state.update_data(kind=kind)
    await state.set_state(AdminUI.fzr_search)
    await call.message.edit_text(
        f"🔎 <b>FazerCard · {_FZR_KINDS[kind]}</b>\n\n"
        "Пришлите название или его часть — например <code>amazon</code>, "
        "<code>steam</code>, <code>pubg</code>.",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.fzr_search)
async def msg_fzr_search(
    message: Message, db: Database, state: FSMContext, fzr: FazerCardClient
) -> None:
    data = await state.get_data()
    pid, kind = data.get("pid"), data.get("kind")
    query = (message.text or "").strip().lower()
    if not query:
        await message.answer("Пришлите слово для поиска.")
        return
    try:
        cats = await asyncio.to_thread(fzr.all_categories, kind)
    except FazerCardError as e:
        await message.answer(f"Ошибка каталога FazerCard: {e}")
        return
    matched = [
        c for c in cats
        if query in str(c.get("name") or "").lower()
        or query in str(c.get("id") or "").lower()
    ]
    if not matched:
        await message.answer("Ничего не найдено. Попробуйте другое слово.")
        return
    matched = matched[:_FZR_RESULTS]
    await state.update_data(cats=matched)
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(matched):
        kb.row(_btn(c.get("name") or str(c.get("id")), f"a_fzrc:{i}"))
    kb.row(_btn("⬅️ Назад", f"a_prod:{pid}"))
    more = "" if len(matched) < _FZR_RESULTS else "\n(первые 20 — уточните запрос)"
    await message.answer(
        f"Выберите категорию:{more}", reply_markup=kb.as_markup()
    )


@router.callback_query(AdminUI.fzr_search, F.data.startswith("a_fzrc:"))
async def cb_fzr_cat(call: CallbackQuery, state: FSMContext, fzr: FazerCardClient) -> None:
    data = await state.get_data()
    cats = data.get("cats") or []
    i = int(call.data.split(":", 1)[1])
    if i >= len(cats):
        await call.answer("Список устарел, начните заново", show_alert=True)
        return
    cat, kind, pid = cats[i], data.get("kind"), data.get("pid")
    try:
        offers = await asyncio.to_thread(fzr.offers_for, kind, cat["id"])
    except FazerCardError as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)
        return
    offers = [o for o in offers if o.get("id")][:_FZR_OFFERS]
    if not offers:
        await call.answer("Нет доступных номиналов", show_alert=True)
        return
    await state.update_data(cat=cat, offers=offers)
    kb = InlineKeyboardBuilder()
    for j, o in enumerate(offers):
        label = o.get("name") or str(o.get("id"))
        extra = []
        if o.get("price_usd"):
            try:
                extra.append(f"${o['price_usd']} (≈{texts.rub(usd_to_rub(Decimal(str(o['price_usd']))))})")
            except Exception:  # noqa: BLE001
                extra.append(f"${o['price_usd']}")
        if o.get("stock") is not None:
            extra.append(f"сток {o['stock']}")
        if extra:
            label += " · " + " · ".join(extra)
        kb.row(_btn(label, f"a_fzro:{j}"))
    kb.row(_btn("⬅️ Назад", f"a_prod:{pid}"))
    await call.message.edit_text(
        f"🎯 <b>{cat.get('name')}</b>\nВыберите номинал (цена — закуп у FazerCard):",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(AdminUI.fzr_search, F.data.startswith("a_fzro:"))
async def cb_fzr_offer(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    offers, cat = data.get("offers") or [], data.get("cat") or {}
    j = int(call.data.split(":", 1)[1])
    if j >= len(offers):
        await call.answer("Список устарел, начните заново", show_alert=True)
        return
    offer, pid = offers[j], data.get("pid")
    await state.update_data(offer=offer)
    await state.set_state(AdminUI.fzr_price)
    price = offer.get("price_usd") or "?"
    try:
        rub_hint = f" (≈ {texts.rub(usd_to_rub(Decimal(str(price))))})"
    except Exception:  # noqa: BLE001
        rub_hint = ""
    await call.message.edit_text(
        f"💲 <b>{cat.get('name')} · {offer.get('name')}</b>\n"
        f"Закуп у FazerCard: <b>${price}</b>{rub_hint}\n\n"
        "Пришлите вашу <b>цену продажи в рублях</b> (например <code>299</code>) "
        "или <code>-</code>, чтобы продавать по закупу.\n"
        "Цена плавает по курсу (пересчитается автоматически при его изменении).",
        reply_markup=_cancel_kb(f"a_prod:{pid}"),
    )
    await call.answer()


@router.message(AdminUI.fzr_price)
async def msg_fzr_price(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid, kind = data.get("pid"), data.get("kind")
    cat, offer = data.get("cat"), data.get("offer")
    if not (cat and offer):
        await state.clear()
        await message.answer("Сессия устарела, начните заново.")
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        cost = Decimal(str(offer.get("price_usd") or "0"))
    except InvalidOperation:
        cost = Decimal("0")
    # Цена вводится в рублях → храним USDT по курсу (плавает).
    price = None
    if raw != "-":
        try:
            rub = Decimal(raw)
        except InvalidOperation:
            await message.answer("Число или -")
            return
        rate = rates_service.get_rate()
        price = (rub / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    title = f"{cat.get('name')} · {offer.get('name')}".strip(" ·")
    edit_vid = data.get("edit_vid")
    async with db.session() as session:
        if edit_vid:
            # Переключаем источник существующего номинала на FazerCard.
            v = await session.get(Variant, edit_vid)
            if not v:
                await state.clear()
                await message.answer("Номинал не найден.")
                return
            v.source = "fazercard"
            v.fzr_kind = kind
            v.fzr_a = str(cat.get("id"))
            v.fzr_b = str(offer.get("id"))
            v.cost_usd = cost
            if raw != "-":
                v.price_usd = price
            await session.commit()
            vid, pid = v.id, v.product_id
            verb = "переключён на FazerCard"
        else:
            min_vid = await session.scalar(
                select(func.min(Variant.liog_variation_id)).where(Variant.product_id == pid)
            )
            new_vid = min(0, int(min_vid or 0)) - 1
            v = Variant(
                product_id=pid, title=title[:255],
                liog_product_id=0, liog_variation_id=new_vid,
                cost_usd=cost, price_usd=price,
                source="fazercard", fzr_kind=kind,
                fzr_a=str(cat.get("id")), fzr_b=str(offer.get("id")),
            )
            session.add(v)
            await session.commit()
            vid = v.id
            verb = "создан"
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    note = ""
    if kind == "topup":
        note = (
            "\nℹ️ Топап: при покупке бот сам спросит у клиента данные игрока "
            "(напр. ID), затем оформит заказ и зачислит на аккаунт."
        )
    await message.answer(
        f"✅ Номинал FazerCard {verb}: <b>{title}</b>" + note,
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("a_vtitle:"))
async def cb_var_title(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_var_title)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "✏️ Пришлите новое <b>название номинала</b> (покороче, чтобы на телефоне "
        "было видно цену).",
        reply_markup=_cancel_kb(f"a_var:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_var_title)
async def msg_var_title(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название пустое. Пришлите текст.")
        return
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.title = title[:255]
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer("✅ Название номинала обновлено", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_setrub:"))
async def cb_set_rub(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_price_rub)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "💱 Пришлите цену в рублях (например <code>199</code> или <code>199.90</code>) "
        "— она показывается рядом с ценой в $.\n"
        "<code>-</code> — убрать цену ₽ (тогда покажется только $).",
        reply_markup=_cancel_kb(f"a_var:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_price_rub)
async def msg_set_rub(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    raw = (message.text or "").strip().replace(",", ".")
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await state.clear()
            await message.answer("Номинал не найден")
            return
        if raw == "-":
            v.price_rub = None
            v.price_mode = None
        else:
            try:
                v.price_rub = Decimal(raw)
            except InvalidOperation:
                await message.answer("Число или -")
                return
            v.price_mode = smartprice_service.MODE_FIXED  # фикс ₽ — не меняется
            v.price_usd = None
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer(
        "✅ Фикс-цена ₽ установлена (не меняется от закупа и курса).",
        reply_markup=kb.as_markup(),
    )


# ── Редактируемые тексты «ожидание»/«выдача» ────────────────────────────────
# Наследование: номинал → товар → родительская категория → дефолт.
#   обычный текст — задать своё значение;
#   «0» — сбросить (наследовать с уровня выше / дефолт);
#   «-» — скрыть (пустой текст, наследование останавливается).
_TXT_FIELD = {"pend": "pending_text", "deliv": "delivered_text"}
_TXT_LABEL = {"pend": "⏳ текст ожидания", "deliv": "✅ текст выдачи"}


def _txt_default(field: str) -> str:
    return (
        texts.PENDING_NOTE_DEFAULT if field == "pending_text"
        else texts.TOPUP_ACCOUNT_DELIVERED
    )


@router.callback_query(F.data.startswith("a_txt:"))
async def cb_set_text(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    # a_txt:{kind p|v}:{field pend|deliv}:{id}
    _, kind, fkey, raw_id = call.data.split(":", 3)
    obj_id = int(raw_id)
    field = _TXT_FIELD.get(fkey)
    if field is None or kind not in ("p", "v"):
        await call.answer("Неизвестно", show_alert=True)
        return
    back = f"a_var:{obj_id}" if kind == "v" else f"a_prod:{obj_id}"
    default = _txt_default(field)
    async with db.session() as session:
        obj = await session.get(Variant if kind == "v" else Product, obj_id)
        if not obj:
            await call.answer("Не найдено", show_alert=True)
            return
        own = getattr(obj, field, None)  # значение именно на этом уровне
        # Текст, который реально действует сейчас (с учётом наследования).
        if kind == "v":
            effective = await catalog_service.resolve_text(session, obj, field, default)
        else:
            effective = await catalog_service.resolve_text_for_product(
                session, obj, field, default
            )
    # Откуда берётся действующий текст.
    if own is not None:
        origin = "" if own != "" else " (скрыт на этом уровне)"
    else:
        origin = " (наследуется / по умолчанию)"
    if effective == "":
        cur_line = f"сейчас: <i>ничего не отправляется{origin}</i>"
    else:
        cur_line = f"сейчас{origin}:\n<blockquote>{effective}</blockquote>"
    await state.set_state(AdminUI.set_text)
    await state.update_data(txt_kind=kind, txt_field=field, txt_id=obj_id)
    await call.message.edit_text(
        f"✏️ Редактирование: <b>{_TXT_LABEL[fkey]}</b>\n{cur_line}\n\n"
        "Пришлите новый текст. Можно с премиум-эмодзи и форматированием "
        "(жирный, курсив) — они сохранятся.\n"
        "<code>0</code> — сбросить (наследовать с уровня выше или дефолт).\n"
        "<code>-</code> — скрыть (ничего не отправлять покупателю).",
        reply_markup=_cancel_kb(back),
    )
    await call.answer()


@router.message(AdminUI.set_text)
async def msg_set_text(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data.get("txt_kind")
    field = data.get("txt_field")
    obj_id = data.get("txt_id")
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пусто. Пришлите текст, либо 0 (сброс) / - (скрыть).")
        return
    if raw == "0":
        value, note = None, "сброшен (наследуется / по умолчанию)"
    elif raw == "-":
        value, note = "", "скрыт — покупателю ничего не отправляется"
    else:
        # html_text сохраняет премиум-эмодзи (custom_emoji) и форматирование.
        value, note = (message.html_text or message.text).strip(), "обновлён"
    back = f"a_var:{obj_id}" if kind == "v" else f"a_prod:{obj_id}"
    async with db.session() as session:
        obj = await session.get(Variant if kind == "v" else Product, obj_id)
        if not obj:
            await state.clear()
            await message.answer("Объект не найден")
            return
        setattr(obj, field, value)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Назад", back))
    await message.answer(f"✅ Текст {note}", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_setprice:"))
async def cb_set_price(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_price)
    await state.update_data(vid=vid)
    rate = rates_service.get_rate()
    await call.message.edit_text(
        f"💰 Пришлите цену продажи <b>в рублях</b> (например <code>299</code>).\n"
        f"Курс сейчас: 1 USDT = {_fmt_rub(rate)} ₽.\n\n"
        "• Для товаров поставщика (FazerCard) включится <b>умная цена</b>: "
        "наценка зафиксируется и цена будет сама плавать от закупа (порог 1%).\n"
        "• Для своего стока — цена плавает по курсу.\n"
        "<code>-</code> — убрать (считать по наценке).",
        reply_markup=_cancel_kb(f"a_var:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_price)
async def msg_set_price(
    message: Message, db: Database, config: BotConfig, state: FSMContext,
    fzr: FazerCardClient,
) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    raw = (message.text or "").strip().replace(",", ".")
    rate = rates_service.get_rate()
    note = "(плавает по курсу)."
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await state.clear()
            await message.answer("Номинал не найден")
            return
        if raw == "-":
            v.price_usd = None
            v.price_rub = None
            v.price_mode = None
        else:
            try:
                rub = Decimal(raw)
            except InvalidOperation:
                await message.answer("Число или -")
                return
            cost = None
            if smartprice_service.supports_smart(v):
                cost = await smartprice_service.live_cost(fzr, v)
            if cost and cost > 0:
                # Умная цена: фиксируем наценку от живого закупа, ₽ — «липкая».
                v.price_mode = smartprice_service.MODE_SMART
                v.markup_percent = smartprice_service.derive_markup(rub, cost, rate)
                v.cost_usd = cost
                v.price_rub = round_price_rub(rub)
                v.price_usd = None
                note = (
                    f"— умная цена: наценка {_fmt_rub(v.markup_percent)}%, "
                    "дальше плавает от закупа."
                )
            else:
                # Нет живого закупа → цена плавает по курсу (как раньше).
                v.price_usd = (rub / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                v.price_rub = None
                v.price_mode = smartprice_service.MODE_FLOAT
        await session.commit()
        client_rub = price_rub_value(v, config.default_markup_percent)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer(
        f"✅ Цена обновлена. Клиент видит: <b>{texts.rub(client_rub)}</b> {note}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("a_setmarkup:"))
async def cb_set_markup(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_markup)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "📈 Пришлите наценку в % (например <code>20</code>) "
        "или <code>-</code>, чтобы вернуть наценку по умолчанию.",
        reply_markup=_cancel_kb(f"a_var:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.set_markup)
async def msg_set_markup(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    raw = (message.text or "").strip().replace(",", ".")
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if not v:
            await state.clear()
            await message.answer("Номинал не найден")
            return
        if raw == "-":
            v.markup_percent = None
        else:
            try:
                v.markup_percent = Decimal(raw)
            except InvalidOperation:
                await message.answer("Число или -")
                return
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer("✅ Наценка обновлена", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_astock:"))
async def cb_add_stock(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.add_stock)
    await state.update_data(vid=vid)
    await call.message.edit_text(
        "📥 Пришлите коды для стока — по одному в строке.",
        reply_markup=_cancel_kb(f"a_var:{vid}"),
    )
    await call.answer()


@router.message(AdminUI.add_stock)
async def msg_add_stock(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    codes = [ln for ln in (message.text or "").splitlines() if ln.strip()]
    if not codes:
        await message.answer("Пусто. Пришлите коды построчно или нажмите Отмена.")
        return
    async with db.session() as session:
        added, skipped = await stock_service.add_codes(session, vid, codes)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer(
        f"✅ Добавлено: {added}" + (f", пропущено дублей: {skipped}" if skipped else ""),
        reply_markup=kb.as_markup(),
    )


# ── Эмодзи товара/номинала ───────────────────────────────────────────────────
_EMOJI_HINT = (
    "🙂 Пришлите <b>одно премиум-эмодзи</b> (нужен Telegram Premium) "
    "или его <b>ID</b> числом (например <code>5406705291213417505</code>), "
    "чтобы поставить иконкой кнопки, или <code>-</code> чтобы убрать.\n"
    "Обычные (не премиум) эмодзи иконкой кнопки Telegram не принимает."
)


@router.callback_query(F.data.startswith("a_pemoji:"))
async def cb_prod_emoji(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_prod_emoji)
    await state.update_data(pid=pid)
    await call.message.edit_text(_EMOJI_HINT, reply_markup=_cancel_kb(f"a_prod:{pid}"))
    await call.answer()


@router.message(AdminUI.set_prod_emoji)
async def msg_prod_emoji(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    pid = data.get("pid")
    raw = (message.text or "").strip()
    emoji_id = None if raw == "-" else _emoji_id_from(message)
    if raw != "-" and emoji_id is None:
        await message.answer("Не нашёл эмодзи. Пришлите премиум-эмодзи, его ID числом или - ")
        return
    async with db.session() as session:
        p = await session.get(Product, pid)
        if p:
            p.icon_emoji_id = emoji_id
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К товару", f"a_prod:{pid}"))
    await message.answer("✅ Эмодзи обновлён" if emoji_id else "✅ Эмодзи убран", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("a_vemoji:"))
async def cb_var_emoji(call: CallbackQuery, state: FSMContext) -> None:
    vid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminUI.set_var_emoji)
    await state.update_data(vid=vid)
    await call.message.edit_text(_EMOJI_HINT, reply_markup=_cancel_kb(f"a_var:{vid}"))
    await call.answer()


@router.message(AdminUI.set_var_emoji)
async def msg_var_emoji(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    vid = data.get("vid")
    raw = (message.text or "").strip()
    emoji_id = None if raw == "-" else _emoji_id_from(message)
    if raw != "-" and emoji_id is None:
        await message.answer("Не нашёл эмодзи. Пришлите премиум-эмодзи, его ID числом или - ")
        return
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v:
            v.icon_emoji_id = emoji_id
            await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К номиналу", f"a_var:{vid}"))
    await message.answer("✅ Эмодзи обновлён" if emoji_id else "✅ Эмодзи убран", reply_markup=kb.as_markup())


# ── Заказы / сток ────────────────────────────────────────────────────────────
@router.callback_query(F.data == "a_orders")
async def cb_orders(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    async with db.session() as session:
        rows = await session.execute(
            select(Order, Variant.title, User.username)
            .join(Variant, Variant.id == Order.variant_id)
            .join(User, User.id == Order.user_id, isouter=True)
            .order_by(Order.id.desc())
            .limit(15)
        )
        items = rows.all()
    if not items:
        text = "🧾 Заказов пока нет."
    else:
        lines = ["🧾 <b>Последние заказы</b>", ""]
        for o, vtitle, username in items:
            total = Decimal(o.price_usd) * (o.quantity or 1)
            who = f"@{username}" if username else f"id {o.user_id}"
            lines.append(
                f"#{o.id} · {vtitle} ×{o.quantity or 1} · "
                f"{texts.rub(total)} · {o.status} · {who} (<code>{o.user_id}</code>)"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "a_stock")
async def cb_stock(call: CallbackQuery, db: Database) -> None:
    async with db.session() as session:
        rows = await session.execute(
            select(Variant.title, func.count(StockItem.id))
            .select_from(Variant)
            .join(
                StockItem,
                (StockItem.variant_id == Variant.id)
                & (StockItem.status == StockStatus.AVAILABLE),
                isouter=True,
            )
            .group_by(Variant.id)
            .order_by(Variant.id)
        )
        items = rows.all()
    if not items:
        text = "📥 Номиналов нет."
    else:
        lines = ["📥 <b>Сток (доступно)</b>", ""]
        for title, cnt in items:
            lines.append(f"{title}: <b>{cnt or 0}</b>")
        text = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Назад", "a_home"))
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()

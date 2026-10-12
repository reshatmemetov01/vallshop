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
from ..services import catalog as catalog_service
from ..services import merchants as merchants_service
from ..services import mprice as mprice_service
from ..services.pricing import price_rub_value
from .. import texts

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

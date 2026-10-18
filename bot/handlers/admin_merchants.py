"""Админ-раздел «Мерчанты»: список франшиз, статус, статистика, выплаты, удаление."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Merchant, TopUp, TopUpStatus, User
from ..services import merchants as merchants_service
from ..services import payouts as payouts_service
from ..services import stats as stats_service
from .. import texts

router = Router(name="admin_merchants")

_FAR = datetime(2100, 1, 1)


class MchFlow(StatesGroup):
    reject_reason = State()   # data: payout_id


class IsAdminCb(BaseFilter):
    async def __call__(self, call: CallbackQuery, config: BotConfig) -> bool:
        return bool(call.from_user and config.is_admin(call.from_user.id))


class IsAdminMsg(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _show_list(call: CallbackQuery, db: Database) -> None:
    merchants = await merchants_service.list_all(db)
    kb = InlineKeyboardBuilder()
    if not merchants:
        caption = "🏪 <b>Мерчанты</b>\n\nФраншиз пока нет."
    else:
        caption = f"🏪 <b>Мерчанты</b>\n\nВсего франшиз: <b>{len(merchants)}</b>\nВыберите для управления:"
        for m in merchants:
            mark = "🟢" if m.status == "active" else "⏸"
            kb.row(_btn(f"{mark} @{m.bot_username or m.id}", f"mch:{m.id}"))
    kb.row(_btn("⬅️ Назад", "rf_home"))
    try:
        await call.message.edit_text(caption, reply_markup=kb.as_markup())
    except Exception:  # noqa: BLE001
        await call.message.answer(caption, reply_markup=kb.as_markup())


async def _show_detail(call: CallbackQuery, db: Database, mid: int) -> None:
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    st = await merchants_service.merchant_stats(db, mid)
    status_ru = "🟢 активна" if m.status == "active" else "⏸ приостановлена"
    caption = (
        f"🏪 <b>Франшиза #{m.id}</b>\n\n"
        f"Бот: @{m.bot_username or '—'}\n"
        f"Админ: @{m.admin_username or '—'} (id <code>{m.admin_user_id}</code>)\n"
        f"Статус: {status_ru}\n\n"
        f"Клиентов: <b>{st['users']}</b>\n"
        f"Заказов: <b>{st['orders']}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn("📊 Полная статистика", f"mch_stats:{m.id}"))
    kb.row(_btn("💸 Выплаты мерчанта", f"mch_pay:{m.id}"))
    if m.status == "active":
        kb.row(_btn("⏸ Приостановить", f"mch_tog:{m.id}"))
    else:
        kb.row(_btn("🟢 Активировать", f"mch_tog:{m.id}"))
    kb.row(_btn("🗑 Удалить франшизу", f"mch_del:{m.id}"))
    kb.row(_btn("⬅️ К списку", "mch_home"))
    await call.message.edit_text(caption, reply_markup=kb.as_markup())


# ── Полная статистика мерчанта (снимок за всё время) ─────────────────────────
@router.callback_query(F.data.startswith("mch_stats:"))
async def cb_mch_stats(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    async with db.session() as s:
        u = await stats_service.users_stats(s, None, _FAR, mid)
        sales = await stats_service.sales_stats(s, None, _FAR, mid)
        bal = await stats_service.total_balance(s, mid)
        topups = list(await s.scalars(
            select(TopUp).where(TopUp.merchant_id == mid).order_by(TopUp.id.desc()).limit(30)
        ))
        m = await s.get(Merchant, mid)
    paid = sum((1 for t in topups if t.status == TopUpStatus.PAID), 0)
    paid_sum = sum((Decimal(t.amount_usd or 0) for t in topups if t.status == TopUpStatus.PAID), Decimal(0))
    caption = (
        f"📊 <b>Статистика франшизы</b> (@{m.bot_username or mid})\n"
        "Период: Всё время\n\n"
        f"👥 Клиентов: <b>{u['total']}</b>\n"
        f"💰 Сумма балансов клиентов: <b>{texts.rub(bal)}</b>\n\n"
        f"🛒 Заказов: <b>{sales['orders']}</b> · товаров: <b>{sales['items']}</b>\n"
        f"Выручка: <b>{texts.rub(sales['revenue'])}</b>\n"
        f"Себестоимость (цена владельца): <b>{texts.rub(sales['cost'])}</b>\n"
        f"Прибыль мерчанта (маржа): <b>{texts.rub(sales['profit'])}</b>\n\n"
        f"💳 Последние пополнения ({len(topups)}, оплачено {paid} на {texts.rub(paid_sum)}):"
    )
    lines = [caption]
    if not topups:
        lines.append("— пополнений нет")
    else:
        st_ru = {TopUpStatus.PAID: "✅", TopUpStatus.PENDING: "⏳", TopUpStatus.EXPIRED: "✖️"}
        for t in topups[:30]:
            d = t.created_at.strftime("%d.%m %H:%M") if t.created_at else "—"
            mark = st_ru.get(t.status, "·")
            lines.append(f"{mark} {texts.rub(Decimal(t.amount_usd or 0))} · user {t.user_id} · {d}")
    kb = InlineKeyboardBuilder().row(_btn("⬅️ Назад", f"mch:{mid}"))
    await call.message.edit_text("\n".join(lines)[:4096], reply_markup=kb.as_markup())
    await call.answer()


# ── Выплаты мерчанта (заявки на вывод) ───────────────────────────────────────
async def _show_payouts(call: CallbackQuery, db: Database, mid: int) -> None:
    async with db.session() as s:
        m = await s.get(Merchant, mid)
        w = await payouts_service.wallets(s, mid)
        pending = await payouts_service.list_requests(s, mid, status="pending")
        recent = await payouts_service.list_requests(s, mid, limit=10)
    total = Decimal(m.earnings_total_rub or 0) if m else Decimal(0)
    avail = Decimal(m.earnings_rub or 0) if m else Decimal(0)
    sbp = payouts_service.wallet_label("sbp", w) if w["sbp"] else "—"
    usdt = payouts_service.wallet_label("usdt", w) if w["usdt"] else "—"
    lines = [
        f"💸 <b>Выплаты франшизы</b> (@{m.bot_username or mid})\n",
        f"Заработано всего: <b>{texts.rub(total)}</b>",
        f"Доступно к выводу: <b>{texts.rub(avail)}</b>\n",
        f"💳 СБП: {sbp}",
        f"🪙 USDT: {usdt}\n",
        f"Заявок в ожидании: <b>{len(pending)}</b>",
    ]
    kb = InlineKeyboardBuilder()
    for p in pending:
        d = p.created_at.strftime("%d.%m.%Y %H:%M") if p.created_at else "—"
        lines.append(f"\n№{p.id} · {texts.rub(p.amount_rub)} · {p.wallet} · {d}")
        kb.row(_btn(f"✅ Одобрить №{p.id}", f"mchp_ok:{p.id}"),
               _btn(f"❌ Отклонить №{p.id}", f"mchp_no:{p.id}"))
    # История последних решений (кратко)
    done = [p for p in recent if p.status != "pending"]
    if done:
        lines.append("\nПоследние решения:")
        st_ru = {"approved": "✅ выполнена", "rejected": "❌ отклонена"}
        for p in done[:5]:
            lines.append(f"№{p.id} · {texts.rub(p.amount_rub)} · {st_ru.get(p.status, p.status)}")
    kb.row(_btn("⬅️ Назад", f"mch:{mid}"))
    await call.message.edit_text("\n".join(lines)[:4096], reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("mch_pay:"))
async def cb_mch_pay(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_payouts(call, db, int(call.data.split(":", 1)[1]))
    await call.answer()


async def _notify_franchise(bots_by_merchant, merchant, text: str) -> None:
    """Уведомить владельца франшизы через её бот."""
    if not bots_by_merchant or merchant is None or not merchant.admin_user_id:
        return
    bot = bots_by_merchant.get(merchant.id)
    if bot is None:
        return
    try:
        await bot.send_message(merchant.admin_user_id, text)
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("mchp_ok:"))
async def cb_payout_approve(call: CallbackQuery, db: Database,
                            bots_by_merchant: dict = None) -> None:
    pid = int(call.data.split(":", 1)[1])
    async with db.session() as s:
        p = await payouts_service.approve(s, pid)
        if p is None:
            await call.answer("Заявка уже обработана", show_alert=True)
            return
        await s.commit()
        mid, amount, wallet = p.merchant_id, p.amount_rub, p.wallet
        m = await s.get(Merchant, mid)
    await _notify_franchise(
        bots_by_merchant, m,
        f"✅ Ваша выплата №{pid} на сумму {texts.rub(amount)} выполнена.\nРеквизиты: {wallet}",
    )
    await _show_payouts(call, db, mid)
    await call.answer("Одобрено ✅")


@router.callback_query(F.data.startswith("mchp_no:"))
async def cb_payout_reject_ask(call: CallbackQuery, state: FSMContext) -> None:
    pid = int(call.data.split(":", 1)[1])
    await state.set_state(MchFlow.reject_reason)
    await state.update_data(payout_id=pid)
    b = InlineKeyboardBuilder().row(_btn("⬅️ Отмена", "mch_home"))
    await call.message.edit_text(
        f"❌ Отклонение заявки №{pid}.\n\nПришлите причину отклонения — она уйдёт франшизе, "
        "а сумма вернётся на её баланс.",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.message(MchFlow.reject_reason)
async def msg_payout_reject(message: Message, db: Database, state: FSMContext,
                            bots_by_merchant: dict = None) -> None:
    data = await state.get_data()
    pid = int(data.get("payout_id"))
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Введите причину отклонения.")
        return
    async with db.session() as s:
        p = await payouts_service.reject(s, pid, reason)
        if p is None:
            await state.clear()
            await message.answer("Заявка уже обработана.")
            return
        await s.commit()
        mid, amount = p.merchant_id, p.amount_rub
        m = await s.get(Merchant, mid)
    await state.clear()
    await _notify_franchise(
        bots_by_merchant, m,
        f"❌ Ваша заявка №{pid} на вывод {texts.rub(amount)} отклонена.\n"
        f"Причина: {reason}\nСумма возвращена на баланс.",
    )
    kb = InlineKeyboardBuilder().row(_btn("⬅️ К выплатам", f"mch_pay:{mid}")).as_markup()
    await message.answer(f"Заявка №{pid} отклонена, сумма возвращена франшизе.", reply_markup=kb)


@router.callback_query(F.data == "mch_home")
async def cb_list(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await _show_list(call, db)
    await call.answer()


@router.callback_query(F.data.startswith("mch:"))
async def cb_detail(call: CallbackQuery, db: Database) -> None:
    await _show_detail(call, db, int(call.data.split(":", 1)[1]))
    await call.answer()


@router.callback_query(F.data.startswith("mch_tog:"))
async def cb_toggle(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    await merchants_service.set_status(db, mid, "suspended" if m.status == "active" else "active")
    await _show_detail(call, db, mid)
    await call.answer("Готово. Вступит в силу после перезапуска сервиса.", show_alert=True)


@router.callback_query(F.data.startswith("mch_del:"))
async def cb_delete_ask(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    m = await merchants_service.get_merchant(db, mid)
    if m is None:
        await call.answer("Не найдено", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить со всеми данными", f"mch_delok:{mid}"))
    kb.row(_btn("⬅️ Отмена", f"mch:{mid}"))
    await call.message.edit_text(
        f"🗑 Удалить франшизу <b>@{m.bot_username or m.id}</b>?\n\n"
        "Будут удалены ВСЕ её данные: клиенты, балансы, заказы, пополнения, отзывы.\n"
        "Это необратимо. Бот перестанет работать после перезапуска сервиса.",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("mch_delok:"))
async def cb_delete(call: CallbackQuery, db: Database) -> None:
    mid = int(call.data.split(":", 1)[1])
    ok = await merchants_service.delete_merchant(db, mid)
    await call.answer("Франшиза удалена ✅" if ok else "Не удалось", show_alert=True)
    await _show_list(call, db)

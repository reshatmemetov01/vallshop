"""Движок управления бонусами: промокоды + бонус-рассылки (дропы).

Используется головным (раздел «Статистика» → «Бонусы») и франшизой (кабинет →
«Бонусы») через :func:`register`, аналогично broadcast_flow. Отличия — префикс
колбэков, точка возврата, набор разделов для кнопки успеха, FSM-группа.

Черновик дропа — в FSM-данных: d_text, d_photo, d_amount, d_limit, d_btnlabel,
d_success, d_sbkind/d_sbtarget/d_sblabel (кнопка успеха), d_error.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import bonuses as bonuses_service
from . import broadcast_flow
from .. import keyboards as kb
from .. import texts


def _bt(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _num(raw: str) -> Optional[Decimal]:
    try:
        v = Decimal((raw or "").strip().replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None
    return v if v > 0 else None


def _int(raw: str) -> Optional[int]:
    try:
        v = int((raw or "").strip())
    except ValueError:
        return None
    return v if v >= 0 else None


async def _show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        await call.message.answer(text, reply_markup=markup)


def register(
    router: Router, *, prefix: str, home_cb: str, sections: list, states: type[StatesGroup],
) -> dict:
    P = prefix
    S = states

    def _draft(d: dict) -> dict:
        return {k: d.get(k) for k in (
            "d_text", "d_photo", "d_amount", "d_limit", "d_btnlabel",
            "d_success", "d_sbkind", "d_sbtarget", "d_sblabel", "d_error",
        )}

    # ── Главная «Бонусы» ─────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:home")
    async def cb_home(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        b = InlineKeyboardBuilder()
        b.row(_bt("🎟 Промокоды", f"{P}:pl"))
        b.row(_bt("🎁 Рассылка бонуса", f"{P}:dnew"), _bt("📊 Дропы", f"{P}:ds"))
        b.row(_bt("⬅️ Назад", home_cb))
        await _show(
            call,
            "🎁 <b>Бонусы</b>\n\n"
            "• Промокоды — коды на пополнение баланса.\n"
            "• Рассылка бонуса — пост с кнопкой «забрать» и лимитом активаций.",
            b.as_markup(),
        )
        await call.answer()

    # ── Промокоды ────────────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:pl")
    async def cb_promo_list(call: CallbackQuery, db: Database, state: FSMContext,
                            merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await state.clear()
        async with db.session() as s:
            promos = await bonuses_service.list_promos(s, merchant_id)
        b = InlineKeyboardBuilder()
        b.row(_bt("➕ Создать промокод", f"{P}:pnew"))
        if not promos:
            body = "🎟 <b>Промокоды</b>\n\nПока нет промокодов."
        else:
            lines = ["🎟 <b>Промокоды</b>\n"]
            for p in promos:
                lim = "∞" if not p.max_activations else p.max_activations
                flag = "🟢" if p.active else "🔴"
                lines.append(
                    f"{flag} <code>{p.code}</code> · {texts.rub(p.amount_rub)} · "
                    f"активаций {p.used_count}/{lim}"
                )
                b.row(_bt(f"{'Выключить' if p.active else 'Включить'} {p.code}", f"{P}:pt:{p.id}"))
            body = "\n".join(lines)
        b.row(_bt("⬅️ Назад", f"{P}:home"))
        await _show(call, body, b.as_markup())
        await call.answer()

    @router.callback_query(F.data.startswith(f"{P}:pt:"))
    async def cb_promo_toggle(call: CallbackQuery, db: Database, state: FSMContext,
                              merchant_id: int = HEAD_MERCHANT_ID) -> None:
        pid = int(call.data.split(":")[-1])
        async with db.session() as s:
            p = await bonuses_service.get_promo(s, pid, merchant_id)
        if p is not None:
            await bonuses_service.set_active(db, pid, merchant_id, not p.active)
        await cb_promo_list(call, db, state, merchant_id)

    @router.callback_query(F.data == f"{P}:pnew")
    async def cb_promo_new(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.promo_amount)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Отмена", f"{P}:pl"))
        await _show(call, "🎟 Сумма бонуса промокода в ₽ (например <code>100</code>).", b.as_markup())
        await call.answer()

    @router.message(S.promo_amount)
    async def msg_promo_amount(message: Message, state: FSMContext) -> None:
        amount = _num(message.text or "")
        if amount is None:
            await message.answer("Введите положительную сумму, например 100.")
            return
        await state.update_data(p_amount=str(amount))
        await state.set_state(S.promo_limit)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Отмена", f"{P}:pl"))
        await message.answer(
            "Лимит активаций (сколько всего раз можно активировать).\n"
            "<code>0</code> — без лимита.",
            reply_markup=b.as_markup(),
        )

    @router.message(S.promo_limit)
    async def msg_promo_limit(message: Message, db: Database, state: FSMContext,
                              merchant_id: int = HEAD_MERCHANT_ID) -> None:
        limit = _int(message.text or "")
        if limit is None:
            await message.answer("Введите целое число (0 — без лимита).")
            return
        data = await state.get_data()
        amount = Decimal(data.get("p_amount", "0"))
        await state.clear()
        async with db.session() as s:
            promo = await bonuses_service.create_promo(
                s, merchant_id=merchant_id, created_by=message.from_user.id,
                amount_rub=amount, max_activations=limit,
            )
            await s.commit()
        lim = "без лимита" if not limit else str(limit)
        b = InlineKeyboardBuilder().row(_bt("⬅️ К промокодам", f"{P}:pl"))
        await message.answer(
            f"✅ Промокод создан:\n\n<code>{promo.code}</code>\n"
            f"Бонус: <b>{texts.rub(amount)}</b> · лимит: {lim}\n\n"
            "Клиенты активируют его в Профиле → Промокод.",
            reply_markup=b.as_markup(),
        )

    # ── Бонус-рассылка: визард ───────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:dnew")
    async def cb_drop_new(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(S.d_post)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Отмена", f"{P}:home"))
        await _show(
            call,
            "🎁 <b>Рассылка бонуса</b> (1/6)\n\n"
            "Пришлите текст поста (можно с форматированием и премиум-эмодзи) "
            "или фото с подписью.",
            b.as_markup(),
        )
        await call.answer()

    @router.message(S.d_post)
    async def msg_d_post(message: Message, state: FSMContext) -> None:
        photo = message.photo[-1].file_id if message.photo else None
        text = message.html_text if (message.text or message.caption) else None
        if not photo and not text:
            await message.answer("Пришлите текст или фото.")
            return
        await state.update_data(d_text=text, d_photo=photo)
        await state.set_state(S.d_amount)
        await message.answer("🎁 (2/6) Сумма бонуса за активацию в ₽ (например <code>50</code>).")

    @router.message(S.d_amount)
    async def msg_d_amount(message: Message, state: FSMContext) -> None:
        amount = _num(message.text or "")
        if amount is None:
            await message.answer("Введите положительную сумму, например 50.")
            return
        await state.update_data(d_amount=str(amount))
        await state.set_state(S.d_limit)
        await message.answer("🎁 (3/6) Лимит активаций — сколько человек успеют получить "
                             "(например <code>100</code>).")

    @router.message(S.d_limit)
    async def msg_d_limit(message: Message, state: FSMContext) -> None:
        limit = _int(message.text or "")
        if not limit:  # для дропа лимит обязателен и > 0
            await message.answer("Введите целое число больше 0, например 100.")
            return
        await state.update_data(d_limit=limit)
        await state.set_state(S.d_btnlabel)
        await message.answer("🎁 (4/6) Текст кнопки «забрать» под постом "
                             "(например <code>🎁 Забрать бонус</code>).")

    @router.message(S.d_btnlabel)
    async def msg_d_btnlabel(message: Message, state: FSMContext) -> None:
        label = (message.text or "").strip()[:64]
        if not label:
            await message.answer("Пришлите текст кнопки.")
            return
        await state.update_data(d_btnlabel=label)
        await state.set_state(S.d_success)
        await message.answer("🎁 (5/6) Текст сообщения об успешном зачислении "
                             "(его получит тот, кто успел).")

    @router.message(S.d_success)
    async def msg_d_success(message: Message, state: FSMContext) -> None:
        text = message.html_text if message.text else None
        if not text:
            await message.answer("Пришлите текст сообщения об успехе.")
            return
        await state.update_data(d_success=text)
        await _ask_success_button(message, state)

    async def _ask_success_button(ev, state: FSMContext) -> None:
        await state.set_state(None)
        b = InlineKeyboardBuilder()
        for key, label, _icon in sections:
            b.row(_bt(label, f"{P}:sbsec:{key}"))
        b.row(_bt("🌐 Своя ссылка", f"{P}:sburl"), _bt("Без кнопки", f"{P}:sbnone"))
        txt = "🎁 Кнопка в сообщении об успехе — куда ведёт?"
        if getattr(ev, "message", None) is not None:  # CallbackQuery
            await _show(ev, txt, b.as_markup())
        else:
            await ev.answer(txt, reply_markup=b.as_markup())

    @router.callback_query(F.data.startswith(f"{P}:sbsec:"))
    async def cb_sb_section(call: CallbackQuery, state: FSMContext) -> None:
        key = call.data.split(":")[-1]
        meta = next((s for s in sections if s[0] == key), None)
        if meta is None:
            await call.answer("Неизвестный раздел", show_alert=True)
            return
        await state.update_data(d_sbkind="cb", d_sbtarget=key, d_sblabel=meta[1])
        await state.set_state(S.d_slabel)
        b = InlineKeyboardBuilder().row(_bt("Оставить как есть", f"{P}:sbkeep"))
        await _show(call, f"Текст кнопки (сейчас «{meta[1]}»). Пришлите свой или оставьте.",
                    b.as_markup())
        await call.answer()

    @router.callback_query(F.data == f"{P}:sburl")
    async def cb_sb_url(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.d_surl)
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:sbback"))
        await _show(call, "🌐 Пришлите ссылку (https://…, t.me/… или @username).", b.as_markup())
        await call.answer()

    @router.message(S.d_surl)
    async def msg_sb_url(message: Message, state: FSMContext) -> None:
        url = broadcast_flow._valid_post_url(message.text or "")
        if not url:
            await message.answer("Не похоже на ссылку. Пришлите URL, t.me/… или @username.")
            return
        await state.update_data(d_sbkind="url", d_sbtarget=url, d_sblabel="Открыть")
        await state.set_state(S.d_slabel)
        await message.answer("Текст кнопки (например <code>Перейти</code>).")

    @router.callback_query(F.data == f"{P}:sbback")
    async def cb_sb_back(call: CallbackQuery, state: FSMContext) -> None:
        await _ask_success_button(call, state)
        await call.answer()

    @router.callback_query(F.data == f"{P}:sbnone")
    async def cb_sb_none(call: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(d_sbkind=None, d_sbtarget=None, d_sblabel=None)
        await _ask_error(call, state)
        await call.answer()

    @router.callback_query(F.data == f"{P}:sbkeep")
    async def cb_sb_keep(call: CallbackQuery, state: FSMContext) -> None:
        await _ask_error(call, state)
        await call.answer()

    @router.message(S.d_slabel)
    async def msg_sb_label(message: Message, state: FSMContext) -> None:
        label = (message.text or "").strip()[:64]
        if label:
            await state.update_data(d_sblabel=label)
        await _ask_error(message, state)

    async def _ask_error(ev, state: FSMContext) -> None:
        await state.set_state(S.d_error)
        txt = ("🎁 (6/6) Текст сообщения для тех, кто не успел (лимит исчерпан).\n"
               "<code>0</code> — использовать стандартный:\n\n"
               f"<i>{texts.BONUS_ENDED_DEFAULT}</i>")
        if getattr(ev, "message", None) is not None:  # CallbackQuery
            await _show(ev, txt, InlineKeyboardBuilder().as_markup())
        else:
            await ev.answer(txt)

    @router.message(S.d_error)
    async def msg_d_error(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        err = texts.BONUS_ENDED_DEFAULT if raw == "0" else (message.html_text or raw)
        await state.update_data(d_error=err)
        await state.set_state(None)
        await _preview(message, state)

    async def _draft_summary(d: dict) -> str:
        sb = "нет"
        if d.get("d_sbtarget"):
            sb = f"«{d.get('d_sblabel')}» → {d.get('d_sbtarget')}"
        return (
            "🎁 <b>Проверьте дроп</b>\n\n"
            f"💰 Бонус: <b>{texts.rub(Decimal(d.get('d_amount') or '0'))}</b>\n"
            f"🔢 Лимит активаций: <b>{d.get('d_limit')}</b>\n"
            f"🔘 Кнопка поста: «{d.get('d_btnlabel')}»\n"
            f"✅ Кнопка успеха: {sb}\n"
            f"📷 Фото: {'есть' if d.get('d_photo') else 'нет'}"
        )

    async def _preview(ev, state: FSMContext) -> None:
        d = _draft(await state.get_data())
        # сам пост (как увидят клиенты) — кнопка «забрать» неактивна в превью
        bpost = InlineKeyboardBuilder().row(_bt(d.get("d_btnlabel") or "🎁 Забрать бонус", f"{P}:noop"))
        if d.get("d_photo"):
            await ev.answer_photo(d["d_photo"], caption=d.get("d_text") or None,
                                  reply_markup=bpost.as_markup())
        elif d.get("d_text"):
            await ev.answer(d["d_text"], reply_markup=bpost.as_markup())
        b = InlineKeyboardBuilder()
        b.row(_bt("📤 Разослать", f"{P}:dsend"))
        b.row(_bt("⬅️ Отмена", f"{P}:home"))
        await ev.answer(await _draft_summary(d), reply_markup=b.as_markup())

    @router.callback_query(F.data == f"{P}:noop")
    async def cb_noop(call: CallbackQuery) -> None:
        await call.answer("Это предпросмотр")

    @router.callback_query(F.data == f"{P}:dsend")
    async def cb_drop_send(call: CallbackQuery, db: Database, state: FSMContext,
                           merchant_id: int = HEAD_MERCHANT_ID) -> None:
        d = _draft(await state.get_data())
        if not d.get("d_amount") or not d.get("d_limit"):
            await call.answer("Черновик не готов", show_alert=True)
            return
        from ..services import broadcast as bs
        count = len(await bs.recipients(db, merchant_id))
        b = InlineKeyboardBuilder()
        b.row(_bt("✅ Да, разослать", f"{P}:dgo"))
        b.row(_bt("⬅️ Отмена", f"{P}:home"))
        await _show(
            call,
            f"📤 Разослать бонус <b>{count}</b> клиентам?\n"
            f"Первые <b>{d.get('d_limit')}</b> успевших получат "
            f"<b>{texts.rub(Decimal(d.get('d_amount') or '0'))}</b>.",
            b.as_markup(),
        )
        await call.answer()

    @router.callback_query(F.data == f"{P}:dgo")
    async def cb_drop_go(call: CallbackQuery, db: Database, state: FSMContext,
                         merchant_id: int = HEAD_MERCHANT_ID) -> None:
        d = _draft(await state.get_data())
        await state.clear()
        if not d.get("d_amount") or not d.get("d_limit"):
            await call.answer("Черновик не готов", show_alert=True)
            return
        owner_id = call.from_user.id
        async with db.session() as s:
            drop = await bonuses_service.create_drop(
                s, merchant_id=merchant_id, created_by=owner_id,
                text=d.get("d_text"), photo=d.get("d_photo"),
                btn_label=d.get("d_btnlabel") or "🎁 Забрать бонус",
                amount_rub=Decimal(d.get("d_amount") or "0"), max_claims=int(d.get("d_limit")),
                success_text=d.get("d_success") or texts.BONUS_SUCCESS_DEFAULT,
                error_text=d.get("d_error") or texts.BONUS_ENDED_DEFAULT,
                success_btn_label=d.get("d_sblabel"), success_btn_kind=d.get("d_sbkind"),
                success_btn_target=d.get("d_sbtarget"),
            )
        drop_id = drop.id
        bot = call.message.bot

        async def _job():
            sent = await bonuses_service.deliver_drop(bot, db, drop_id)
            try:
                await bot.send_message(
                    owner_id,
                    f"🎁 Бонус-рассылка #{drop_id} разослана: <b>{sent}</b> получателей.\n"
                    "Пост и служебные сообщения удалятся у всех через 24 часа.",
                )
            except Exception:  # noqa: BLE001
                pass

        asyncio.create_task(_job())
        b = InlineKeyboardBuilder().row(_bt("⬅️ Назад", f"{P}:home"))
        await _show(call, "🎁 Бонус-рассылка запущена. По завершении пришлю отчёт.", b.as_markup())
        await call.answer("Запущено ✅")

    # ── Статистика дропов ────────────────────────────────────────────────────
    @router.callback_query(F.data == f"{P}:ds")
    async def cb_drop_stats(call: CallbackQuery, db: Database, state: FSMContext,
                            merchant_id: int = HEAD_MERCHANT_ID) -> None:
        await state.clear()
        async with db.session() as s:
            drops = await bonuses_service.recent_drops(s, merchant_id, limit=10)
        b = InlineKeyboardBuilder()
        if not drops:
            body = "📊 <b>Бонус-рассылки</b>\n\nПока не было."
        else:
            lines = ["📊 <b>Бонус-рассылки</b>\n"]
            st_ru = {"sending": "⏳", "active": "🟢", "expired": "⚪"}
            for dr in drops:
                lines.append(
                    f"{st_ru.get(dr.status, '')} #{dr.id} · {texts.rub(dr.amount_rub)} · "
                    f"забрали {dr.claimed_count}/{dr.max_claims}"
                )
            body = "\n".join(lines)
        b.row(_bt("⬅️ Назад", f"{P}:home"))
        await _show(call, body, b.as_markup())
        await call.answer()

    return {
        "home": cb_home, "promo_list": cb_promo_list, "promo_new": cb_promo_new,
        "promo_amount": msg_promo_amount, "promo_limit": msg_promo_limit,
        "promo_toggle": cb_promo_toggle, "drop_new": cb_drop_new, "d_post": msg_d_post,
        "d_amount": msg_d_amount, "d_limit": msg_d_limit, "d_btnlabel": msg_d_btnlabel,
        "d_success": msg_d_success, "sb_section": cb_sb_section, "sb_none": cb_sb_none,
        "sb_label": msg_sb_label, "d_error": msg_d_error, "drop_send": cb_drop_send,
        "drop_go": cb_drop_go, "drop_stats": cb_drop_stats,
    }

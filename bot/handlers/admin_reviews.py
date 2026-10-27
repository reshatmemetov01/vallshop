"""Админ-раздел «Отзывы»: модерация — редактировать, опубликовать, удалить."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import merchants as merchants_service
from ..services import reviews as review_service
from .. import texts

router = Router(name="admin_reviews")


async def _can_moderate(user_id: int, config: BotConfig, db: Database, merchant_id: int) -> bool:
    """Доступ к модерации: головной админ в головном боте ИЛИ владелец франшизы в её боте."""
    if merchant_id == HEAD_MERCHANT_ID:
        return config.is_admin(user_id)
    return await merchants_service.admin_merchant(db, user_id, merchant_id) is not None


class IsAdminCb(BaseFilter):
    async def __call__(
        self, call: CallbackQuery, config: BotConfig, db: Database,
        merchant_id: int = HEAD_MERCHANT_ID,
    ) -> bool:
        return bool(call.from_user) and await _can_moderate(
            call.from_user.id, config, db, merchant_id
        )


class IsAdminMsg(BaseFilter):
    async def __call__(
        self, message: Message, config: BotConfig, db: Database,
        merchant_id: int = HEAD_MERCHANT_ID,
    ) -> bool:
        return bool(message.from_user) and await _can_moderate(
            message.from_user.id, config, db, merchant_id
        )


router.callback_query.filter(IsAdminCb())
router.message.filter(IsAdminMsg())


def _scope(merchant_id: int):
    """Область видимости отзывов: None = все (головной), иначе id мерчанта."""
    return None if merchant_id == HEAD_MERCHANT_ID else merchant_id


async def _in_scope(session, review_id: int, scope):
    """Отзыв, если он в области видимости (для франшизы — только её отзывы)."""
    r = await review_service.get(session, review_id)
    if r is None:
        return None
    if scope is not None and r.merchant_id != scope:
        return None
    return r


class ARVState(StatesGroup):
    edit_text = State()
    add_product = State()
    add_username = State()
    add_text = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _stars(rating: int) -> str:
    rating = max(0, min(5, int(rating or 0)))
    return "★" * rating + "☆" * (5 - rating)


_STATUS_RU = {"new": "🆕 новый", "published": "✅ опубликован"}


@router.callback_query(F.data == "arv_home")
async def cb_home(call: CallbackQuery, db: Database, state: FSMContext,
                  merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    scope = _scope(merchant_id)
    async with db.session() as session:
        items = await review_service.list_all(session, limit=40, merchant_id=scope)
        total = await review_service.count_published(session, scope)
    kb = InlineKeyboardBuilder()
    for r in items:
        mark = "✅" if r.status == "published" else "🆕"
        kb.row(_btn(f"{mark} {_stars(r.rating)} · {r.product_title or '—'}", f"arv:{r.id}"))
    kb.row(_btn("➕ Добавить отзыв", "arv_add"))
    kb.row(_btn("⬅️ Назад", "a_home" if scope is None else "ma_home"))
    caption = (
        "⭐ <b>Отзывы</b>\n"
        f"Опубликовано: <b>{total}</b> · всего в списке: <b>{len(items)}</b>\n\n"
        + ("Выберите отзыв для модерации:" if items else "Отзывов пока нет.")
    )
    await call.message.edit_text(caption, reply_markup=kb.as_markup())
    await call.answer()


async def _detail(session, review_id: int) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    r = await review_service.get(session, review_id)
    if not r:
        return None, None
    who = f"@{r.username}" if r.username else f"id {r.user_id}"
    when = r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "—"
    amount = texts.rub(Decimal(r.amount_usd)) if r.amount_usd is not None else "—"
    caption = (
        f"⭐ <b>Отзыв #{r.id}</b> · {_STATUS_RU.get(r.status, r.status)}\n"
        f"Заказ: <code>{r.order_id or '—'}</code>\n"
        f"Дата: {when}\n"
        f"Товар: <b>{r.product_title or '—'}</b>\n"
        f"Покупатель: {who}\n"
        f"Сумма заказа: <b>{amount}</b>\n"
        f"Оценка: {_stars(r.rating)}\n\n"
        f"<b>Текст:</b>\n<blockquote>{r.text or ''}</blockquote>"
    )
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✏️ Редактировать текст", f"arv_edit:{r.id}"))
    if r.status == "published":
        kb.row(_btn("🙈 Снять с публикации", f"arv_unpub:{r.id}"))
    else:
        kb.row(_btn("📢 Опубликовать", f"arv_pub:{r.id}"))
    kb.row(_btn("🗑 Удалить", f"arv_del:{r.id}"))
    kb.row(_btn("⬅️ Назад", "arv_home"))
    return caption, kb.as_markup()


@router.callback_query(F.data.startswith("arv:"))
async def cb_detail(call: CallbackQuery, db: Database, state: FSMContext,
                    merchant_id: int = HEAD_MERCHANT_ID) -> None:
    await state.clear()
    rid = int(call.data.split(":", 1)[1])
    scope = _scope(merchant_id)
    async with db.session() as session:
        if await _in_scope(session, rid, scope) is None:
            await call.answer("Не найдено", show_alert=True)
            return
        caption, markup = await _detail(session, rid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("arv_pub:"))
async def cb_publish(call: CallbackQuery, db: Database,
                     merchant_id: int = HEAD_MERCHANT_ID) -> None:
    rid = int(call.data.split(":", 1)[1])
    scope = _scope(merchant_id)
    async with db.session() as session:
        if await _in_scope(session, rid, scope) is None:
            await call.answer("Не найдено", show_alert=True)
            return
        await review_service.set_status(session, rid, "published")
        await session.commit()
        caption, markup = await _detail(session, rid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Опубликовано ✅")


@router.callback_query(F.data.startswith("arv_unpub:"))
async def cb_unpublish(call: CallbackQuery, db: Database,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
    rid = int(call.data.split(":", 1)[1])
    scope = _scope(merchant_id)
    async with db.session() as session:
        if await _in_scope(session, rid, scope) is None:
            await call.answer("Не найдено", show_alert=True)
            return
        await review_service.set_status(session, rid, "new")
        await session.commit()
        caption, markup = await _detail(session, rid)
    await call.message.edit_text(caption, reply_markup=markup)
    await call.answer("Снято с публикации")


@router.callback_query(F.data.startswith("arv_del:"))
async def cb_delete_ask(call: CallbackQuery, db: Database,
                        merchant_id: int = HEAD_MERCHANT_ID) -> None:
    rid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        if await _in_scope(session, rid, _scope(merchant_id)) is None:
            await call.answer("Не найдено", show_alert=True)
            return
    kb = InlineKeyboardBuilder()
    kb.row(_btn("🗑 Да, удалить", f"arv_delok:{rid}"))
    kb.row(_btn("⬅️ Отмена", f"arv:{rid}"))
    await call.message.edit_text(
        "Удалить этот отзыв? Действие необратимо.", reply_markup=kb.as_markup()
    )
    await call.answer()


@router.callback_query(F.data.startswith("arv_delok:"))
async def cb_delete(call: CallbackQuery, db: Database, state: FSMContext,
                    merchant_id: int = HEAD_MERCHANT_ID) -> None:
    rid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        if await _in_scope(session, rid, _scope(merchant_id)) is None:
            await call.answer("Не найдено", show_alert=True)
            return
        await review_service.delete(session, rid)
        await session.commit()
    await cb_home(call, db, state, merchant_id)


@router.callback_query(F.data.startswith("arv_edit:"))
async def cb_edit_ask(call: CallbackQuery, db: Database, state: FSMContext,
                      merchant_id: int = HEAD_MERCHANT_ID) -> None:
    rid = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        if await _in_scope(session, rid, _scope(merchant_id)) is None:
            await call.answer("Не найдено", show_alert=True)
            return
    await state.set_state(ARVState.edit_text)
    await state.update_data(rid=rid)
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ Отмена", f"arv:{rid}"))
    await call.message.edit_text(
        "✏️ Пришлите новый текст отзыва (можно премиум-эмодзи и форматирование).",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.message(ARVState.edit_text)
async def msg_edit(message: Message, db: Database, state: FSMContext,
                   merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    rid = data.get("rid")
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("Пусто. Пришлите текст.")
        return
    async with db.session() as session:
        if await _in_scope(session, rid, _scope(merchant_id)) is None:
            await message.answer("Отзыв не найден.")
            await state.clear()
            return
        await review_service.update_text(session, rid, text)
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К отзыву", f"arv:{rid}"))
    await message.answer("✅ Текст отзыва обновлён", reply_markup=kb.as_markup())


# ── Ручное добавление отзыва админом ─────────────────────────────────────────
@router.callback_query(F.data == "arv_add")
async def cb_add_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ARVState.add_product)
    await call.message.edit_text(
        "➕ <b>Новый отзыв (вручную)</b>\n\n"
        "Шаг 1/4. Пришлите <b>название товара</b> (как показать в отзыве).",
        reply_markup=InlineKeyboardBuilder().row(_btn("⬅️ Отмена", "arv_home")).as_markup(),
    )
    await call.answer()


@router.message(ARVState.add_product)
async def msg_add_product(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Пусто. Пришлите название товара.")
        return
    await state.update_data(product_title=title[:255])
    kb = InlineKeyboardBuilder()
    for n in (1, 2, 3, 4, 5):
        kb.button(text=str(n), callback_data=f"arvrate:{n}")
    kb.adjust(5)
    kb.row(_btn("⬅️ Отмена", "arv_home"))
    await message.answer("Шаг 2/4. Выберите <b>оценку</b>:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("arvrate:"))
async def cb_add_rating(call: CallbackQuery, state: FSMContext) -> None:
    rating = int(call.data.split(":", 1)[1])
    await state.update_data(rating=rating)
    await state.set_state(ARVState.add_username)
    await call.message.edit_text(
        f"Оценка: {_stars(rating)}\n\n"
        "Шаг 3/4. Пришлите <b>@username</b> покупателя (или <code>-</code>, чтобы без имени).",
        reply_markup=InlineKeyboardBuilder().row(_btn("⬅️ Отмена", "arv_home")).as_markup(),
    )
    await call.answer()


@router.message(ARVState.add_username)
async def msg_add_username(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    username = None if raw in ("", "-") else raw.lstrip("@")[:64]
    await state.update_data(username=username)
    await state.set_state(ARVState.add_text)
    await message.answer(
        "Шаг 4/4. Пришлите <b>текст отзыва</b> (можно премиум-эмодзи).",
        reply_markup=InlineKeyboardBuilder().row(_btn("⬅️ Отмена", "arv_home")).as_markup(),
    )


@router.message(ARVState.add_text)
async def msg_add_text(message: Message, db: Database, state: FSMContext,
                       merchant_id: int = HEAD_MERCHANT_ID) -> None:
    data = await state.get_data()
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("Пусто. Пришлите текст отзыва.")
        return
    async with db.session() as session:
        review = await review_service.create_review(
            session,
            order_id=None,
            user_id=0,  # добавлено вручную админом
            username=data.get("username"),
            variant_id=None,
            product_title=data.get("product_title"),
            amount_usd=None,
            rating=int(data.get("rating", 5)),
            text=text,
            merchant_id=merchant_id,   # отзыв привязывается к магазину-владельцу
        )
        # Ручной отзыв сразу публикуем.
        await review_service.set_status(session, review.id, "published")
        await session.commit()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(_btn("⬅️ К отзывам", "arv_home"))
    await message.answer(
        "✅ Отзыв добавлен и опубликован.", reply_markup=kb.as_markup()
    )

"""Отзывы покупателей (пользовательская часть): оставить отзыв и публичный список."""

from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, Order, OrderStatus, Variant
from ..services import menu as menu_service
from ..services import notify as notify_service
from ..services import reviews as review_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router(name="reviews")


class ReviewFlow(StatesGroup):
    waiting_text = State()


def _stars(rating: int) -> str:
    rating = max(0, min(5, int(rating or 0)))
    return "★" * rating + "☆" * (5 - rating)


# ── Оставить отзыв ────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("review:"))
async def cb_review(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    order_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != call.from_user.id:
            await call.answer("Заказ не найден", show_alert=True)
            return
        if order.status != OrderStatus.COMPLETED:
            await call.answer("Отзыв можно оставить только по выполненному заказу", show_alert=True)
            return
    await state.clear()
    await call.message.answer(
        texts.REVIEW_ASK_RATING, reply_markup=kb.review_rating_kb(order_id)
    )
    await call.answer()


@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(call: CallbackQuery, state: FSMContext) -> None:
    _, raw_oid, raw_n = call.data.split(":", 2)
    order_id, rating = int(raw_oid), int(raw_n)
    await state.set_state(ReviewFlow.waiting_text)
    await state.update_data(order_id=order_id, rating=rating)
    await call.message.answer(texts.REVIEW_ASK_TEXT)
    await call.answer()


@router.message(ReviewFlow.waiting_text)
async def msg_review_text(
    message: Message, db: Database, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    rating = int(data.get("rating", 5))
    text = (message.html_text or message.text or "").strip()
    if not text:
        await message.answer("Пустой отзыв. Напишите текст.")
        return
    await state.clear()
    async with db.session() as session:
        order = await session.get(Order, order_id)
        if not order or order.user_id != message.from_user.id:
            await message.answer("Заказ не найден.")
            return
        variant = await session.get(Variant, order.variant_id)
        product_title = variant.title if variant else None
        review = await review_service.create_review(
            session,
            order_id=order_id,
            user_id=message.from_user.id,
            username=message.from_user.username,
            variant_id=order.variant_id,
            product_title=product_title,
            amount_usd=Decimal(order.price_usd) * (order.quantity or 1),
            rating=rating,
            text=text,
            merchant_id=order.merchant_id,
        )
        review_id = review.id
        await session.commit()

    # Уведомляем админов о новом отзыве.
    await notify_service.notify_admins(
        message.bot, config.admin_ids,
        f"🆕 Новый отзыв #{review_id} ({_stars(rating)}) на «{product_title or '—'}».\n"
        "Админ-панель → Отзывы.",
    )
    await message.answer(texts.REVIEW_THANKS)
    # Сразу возвращаем в главное меню.
    is_admin = config.is_admin(message.from_user.id)
    await render(
        message, banner="main", caption=menu_service.caption("main", texts.START, merchant_id),
        reply_markup=kb.main_menu_kb(is_admin, merchant_id=merchant_id),
    )


# ── Публичный список отзывов ─────────────────────────────────────────────────
def _reviews_page_kb(page: int, total_pages: int) -> "InlineKeyboardBuilder":
    kb_b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"revpage:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(1, total_pages)}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"revpage:{page + 1}"))
    if nav:
        kb_b.row(*nav)
    kb_b.row(InlineKeyboardButton(text="Меню", callback_data="menu"))
    return kb_b


async def _show_reviews(
    call: CallbackQuery, db: Database, page: int,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    async with db.session() as session:
        total = await review_service.count_published(session, merchant_id)
        items = await review_service.list_published(
            session, page=page, merchant_id=merchant_id
        )
    title = menu_service.caption("reviews", texts.REVIEWS_TITLE, merchant_id)
    if total == 0:
        caption = f"{title}\n\n{texts.REVIEWS_EMPTY}"
        kb_b = InlineKeyboardBuilder()
        kb_b.row(InlineKeyboardButton(text="Меню", callback_data="menu"))
        await render(call, banner="reviews", caption=caption, reply_markup=kb_b.as_markup())
        return
    total_pages = (total + review_service.PAGE_SIZE - 1) // review_service.PAGE_SIZE
    lines = [f"{title}", f"Всего отзывов: <b>{total}</b>", ""]
    for r in items:
        who = f"@{r.username}" if r.username else "покупатель"
        lines.append(f"{_stars(r.rating)}  ·  <b>{r.product_title or '—'}</b>")
        lines.append(f"{who}: {r.text or ''}")
        lines.append("")
    caption = "\n".join(lines).strip()
    await render(
        call, banner="reviews", caption=caption,
        reply_markup=_reviews_page_kb(page, total_pages).as_markup(),
    )


@router.callback_query(F.data == "reviews")
async def cb_reviews(
    call: CallbackQuery, db: Database, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    await _show_reviews(call, db, 0, merchant_id)
    await call.answer()


@router.callback_query(F.data.startswith("revpage:"))
async def cb_reviews_page(
    call: CallbackQuery, db: Database, merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    page = int(call.data.split(":", 1)[1])
    await _show_reviews(call, db, max(0, page), merchant_id)
    await call.answer()

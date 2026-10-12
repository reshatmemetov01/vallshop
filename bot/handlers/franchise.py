"""Онбординг франшизы: «Создать франшизу» (Фаза 3, вариант B).

Партнёр создаёт бота в @BotFather и присылает токен. Движок валидирует токен
(getMe), создаёт запись мерчанта в БД и уведомляет владельца платформы. Бот
франшизы поднимается при следующем перезапуске сервиса (вариант B).

TODO (вариант A, позже): поднимать бота франшизы на лету, без перезапуска —
через фиксацию версии aiogram и запуск polling для нового бота в рантайме
(см. ТЗ «Франшиза», раздел про активацию). Сейчас — только через перезапуск.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from ..services import merchants as merchants_service
from ..services import notify as notify_service
from ..ui import render

log = logging.getLogger("vallshop.franchise")

router = Router(name="franchise")


class FranchiseFlow(StatesGroup):
    waiting_token = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


_INFO = (
    "🏪 <b>Франшиза VallShop</b>\n\n"
    "Свой магазин в Telegram — без склада, без разработки и без поддержки "
    "клиентов. Вы продаёте под своим брендом, всё остальное на нас.\n\n"
    "<b>Что нужно от вас:</b>\n"
    "• Бот-магазин: создайте в @BotFather и пришлите его токен\n"
    "• Одна франшиза на аккаунт\n\n"
    "<b>Как вы зарабатываете:</b> наценка на товары — ваша прибыль целиком, "
    "комиссий и абонплаты нет. Заработок копится на счёте франшизы, вывод по запросу.\n\n"
    "Нажмите «Создать франшизу» и пришлите токен бота."
)


def _home_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_btn("✅ Создать франшизу", "fr_create"))
    kb.row(_btn("⬅️ Назад", "earn"))
    return kb.as_markup()


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(_btn("⬅️ Назад", "earn")).as_markup()


async def _already_caption(m) -> str:
    return (
        "🏪 <b>Ваша франшиза</b>\n\n"
        f"Бот: @{m.bot_username or '—'}\n"
        f"Статус: {'🟢 активна' if m.status == 'active' else '⏸ приостановлена'}\n\n"
        "Одна франшиза на аккаунт — новую создать нельзя."
    )


@router.callback_query(F.data == "fr_home")
async def cb_fr_home(
    call: CallbackQuery, db: Database, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    await state.clear()
    if merchant_id != HEAD_MERCHANT_ID:
        await call.answer("Недоступно", show_alert=True)
        return
    existing = await merchants_service.merchant_for_admin(db, call.from_user.id)
    if existing is not None:
        await render(call, banner="main", caption=await _already_caption(existing),
                     reply_markup=_back_kb())
    else:
        await render(call, banner="main", caption=_INFO, reply_markup=_home_kb())
    await call.answer()


@router.callback_query(F.data == "fr_create")
async def cb_fr_create(
    call: CallbackQuery, db: Database, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    if merchant_id != HEAD_MERCHANT_ID:
        await call.answer("Недоступно", show_alert=True)
        return
    existing = await merchants_service.merchant_for_admin(db, call.from_user.id)
    if existing is not None:
        await render(call, banner="main", caption=await _already_caption(existing),
                     reply_markup=_back_kb())
        await call.answer()
        return
    await state.set_state(FranchiseFlow.waiting_token)
    await render(
        call, banner="main",
        caption=(
            "🔑 Пришлите <b>токен бота</b> из @BotFather.\n\n"
            "Выглядит так: <code>1234567890:AA...</code>\n\n"
            "Как получить: откройте @BotFather → /newbot → задайте имя → "
            "скопируйте токен и пришлите его сюда."
        ),
        reply_markup=_back_kb(),
    )
    await call.answer()


@router.message(FranchiseFlow.waiting_token)
async def msg_fr_token(
    message: Message, db: Database, config: BotConfig, state: FSMContext,
    merchant_id: int = HEAD_MERCHANT_ID,
) -> None:
    if merchant_id != HEAD_MERCHANT_ID:
        return
    token = (message.text or "").strip()
    if ":" not in token or len(token) < 20:
        await message.answer("Это не похоже на токен. Пришлите токен из @BotFather.")
        return

    # Повторная защита: одна франшиза на аккаунт + токен не занят.
    if await merchants_service.merchant_for_admin(db, message.from_user.id) is not None:
        await state.clear()
        await message.answer("У вас уже есть франшиза — новую создать нельзя.")
        return
    if token == config.bot_token or await merchants_service.token_in_use(db, token):
        await message.answer("Этот бот уже используется. Создайте нового бота в @BotFather.")
        return

    # Валидация токена через Telegram (getMe).
    test_bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await test_bot.get_me()
    except Exception:  # noqa: BLE001 — неверный/отозванный токен
        await message.answer("Токен не принят Telegram. Проверьте и пришлите снова.")
        return
    finally:
        try:
            await test_bot.session.close()
        except Exception:  # noqa: BLE001
            pass

    title = (me.first_name or me.username or "Магазин").strip()
    merchant = await merchants_service.create_merchant(
        db, title=title, bot_token=token, bot_username=me.username,
        admin_user_id=message.from_user.id, admin_username=message.from_user.username,
    )
    await state.clear()

    # Уведомляем владельца платформы — нужен перезапуск для активации (вариант B).
    try:
        await notify_service.notify_admins(
            message.bot, config.admin_ids,
            "🆕 <b>Новая франшиза</b>\n"
            f"Бот: @{me.username}\n"
            f"Админ: @{message.from_user.username or '—'} (id <code>{message.from_user.id}</code>)\n"
            f"ID франшизы: <code>{merchant.id}</code>\n\n"
            "⚠️ Перезапустите сервис (git pull + ребилд или restart), чтобы бот активировался.",
        )
    except Exception:  # noqa: BLE001
        log.warning("Не удалось уведомить владельца о новой франшизе")

    await message.answer(
        "✅ <b>Франшиза создана!</b>\n\n"
        f"Ваш бот: @{me.username}\n\n"
        "Бот активируется в ближайшее время. После активации откройте его и "
        "нажмите /start — это уже ваш магазин."
    )

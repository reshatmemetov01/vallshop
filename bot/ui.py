"""Единый вывод экранов с баннером-картинкой.

Все основные экраны бота — это фото-сообщение (баннер) с подписью и inline-
кнопками, либо (для франшиз без своего баннера) текстовое сообщение с теми же
кнопками. Навигация редактирует это же сообщение.

Баннер выбирается по магазину (мерчанту), которому принадлежит бот апдейта:
  * у мерчанта загружен свой баннер экрана → его file_id;
  * головной (merchant_id = 0) без своего баннера → стандартный файл-ассет;
  * франшиза без своего баннера → экран без картинки (только текст).

Чтобы не протаскивать merchant_id во все вызовы render(), мерчант определяется
внутри по ``event.bot.id`` через карту, заданную на старте (``configure``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Union

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

log = logging.getLogger("vallshop.ui")

_ASSETS = Path(__file__).parent / "assets"

HEAD_MERCHANT_ID = 0

# Логические имена баннеров -> файл.
BANNERS = {
    "main": _ASSETS / "banner_main.jpg",
    "catalog": _ASSETS / "banner_catalog.jpg",
    "profile": _ASSETS / "banner_profile.jpg",
    "reviews": _ASSETS / "banner_reviews.jpg",
    "faq": _ASSETS / "banner_faq.jpg",
}

# Кеш file_id уже загруженных баннеров-ассетов (ускоряет повторную отправку).
# ВАЖНО: file_id в Telegram привязан к конкретному боту, поэтому ключ —
# (bot_id, banner): у каждого бота франшизы свой file_id для того же баннера.
_file_ids: Dict[tuple, str] = {}

# Карта «бот → мерчант» (задаётся на старте) и персональные баннеры мерчантов
# ((merchant_id, banner) -> file_id, загруженные владельцем франшизы).
_bot_merchant: Dict[int, int] = {}
_banner_over: Dict[tuple, str] = {}

CAPTION_LIMIT = 1024


def configure(bot_merchant: Dict[int, int]) -> None:
    """Задать карту bot_id → merchant_id (вызывается на старте)."""
    _bot_merchant.clear()
    _bot_merchant.update({int(k): int(v) for k, v in bot_merchant.items()})


def set_banner_overrides(overrides: Dict[tuple, str]) -> None:
    """Заменить кэш персональных баннеров: {(merchant_id, banner): file_id}."""
    _banner_over.clear()
    _banner_over.update(overrides)


def set_banner_override(merchant_id: int, banner: str, file_id: Optional[str]) -> None:
    key = (int(merchant_id), banner)
    if file_id:
        _banner_over[key] = file_id
    else:
        _banner_over.pop(key, None)


def _merchant_of(event: Union[CallbackQuery, Message]) -> int:
    return _bot_merchant.get(_bot_id(event), HEAD_MERCHANT_ID)


def _clip(caption: str) -> str:
    return caption if len(caption) <= CAPTION_LIMIT else caption[: CAPTION_LIMIT - 1] + "…"


def _bot_id(event: Union[CallbackQuery, Message]) -> int:
    bot = getattr(event, "bot", None)
    return int(bot.id) if bot is not None else 0


def _resolve_source(
    banner: str, bot_id: int, merchant_id: int, override: Optional[str]
) -> Optional[Union[str, FSInputFile]]:
    """Источник картинки для экрана, либо None (экран без баннера — только текст).

    ``override`` — прямой file_id (напр. баннер категории) используется как есть.
    """
    if override:
        return override
    # Персональный баннер мерчанта (загружен владельцем франшизы).
    own = _banner_over.get((merchant_id, banner))
    if own:
        return own
    if merchant_id == HEAD_MERCHANT_ID:
        # Головной: стандартный ассет (с кешем file_id по боту).
        key = (bot_id, banner)
        if key in _file_ids:
            return _file_ids[key]
        return FSInputFile(str(BANNERS[banner]))
    # Франшиза без своего баннера → без картинки.
    return None


def _remember(banner: str, bot_id: int, msg: Message) -> None:
    try:
        if msg.photo:
            _file_ids[(bot_id, banner)] = msg.photo[-1].file_id
    except Exception:  # noqa: BLE001
        pass


async def _send_text(target, caption, reply_markup):
    return await target.answer(caption, reply_markup=reply_markup)


async def _send_photo(target, banner, bot_id, src, caption, reply_markup, is_asset):
    """Отправить фото-баннер с фолбэком: если file_id не принят (напр. чужой
    от другого бота) — шлём стандартный баннер с диска, иначе хотя бы текст."""
    try:
        sent = await target.answer_photo(src, caption=caption, reply_markup=reply_markup)
        if is_asset:
            _remember(banner, bot_id, sent)
        return sent
    except TelegramBadRequest:
        try:
            sent = await target.answer_photo(
                FSInputFile(str(BANNERS[banner])), caption=caption, reply_markup=reply_markup
            )
            _remember(banner, bot_id, sent)
            return sent
        except Exception:  # noqa: BLE001
            return await target.answer(caption, reply_markup=reply_markup)


async def render(
    event: Union[CallbackQuery, Message],
    *,
    banner: str,
    caption: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    photo: Optional[str] = None,
) -> None:
    """Показать экран. Для callback — редактирует сообщение, для message — новое.

    ``photo`` — прямой Telegram file_id (например, свой баннер категории);
    если задан, используется вместо стандартного баннера ``banner``.
    Баннер выбирается по мерчанту бота; франшиза без своего баннера → только текст.
    """
    caption = _clip(caption)
    bot_id = _bot_id(event)
    merchant_id = _merchant_of(event)
    src = _resolve_source(banner, bot_id, merchant_id, photo)
    is_asset = src is not None and not photo and _banner_over.get((merchant_id, banner)) is None

    if isinstance(event, CallbackQuery):
        msg = event.message
        if src is not None:
            media = InputMediaPhoto(media=src, caption=caption, parse_mode="HTML")
            try:
                edited = await msg.edit_media(media, reply_markup=reply_markup)
                if isinstance(edited, Message) and is_asset:
                    _remember(banner, bot_id, edited)
                return
            except TelegramBadRequest as exc:
                text = str(exc).lower()
                if "message is not modified" in text:
                    return
                # Чужой/битый file_id или текстовое сообщение — удалим и отправим заново.
                try:
                    await msg.delete()
                except Exception:  # noqa: BLE001
                    pass
            target = msg
            await _send_photo(target, banner, bot_id, src, caption, reply_markup, is_asset)
            return
        # Текстовый экран (франшиза без баннера): правим текст; если было фото — пересоздаём.
        try:
            await msg.edit_text(caption, reply_markup=reply_markup, parse_mode="HTML")
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
            await _send_text(msg, caption, reply_markup)
            return

    # message: новое сообщение
    target = event
    if src is not None:
        await _send_photo(target, banner, bot_id, src, caption, reply_markup, is_asset)
    else:
        await _send_text(target, caption, reply_markup)

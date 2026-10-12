"""Единый вывод экранов с баннером-картинкой.

Все основные экраны бота — это фото-сообщение (баннер) с подписью и inline-
кнопками под ним. Навигация редактирует это же сообщение (edit_media /
edit_caption). file_id баннеров кешируется, чтобы не пере-загружать картинку.
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

# Логические имена баннеров -> файл.
BANNERS = {
    "main": _ASSETS / "banner_main.jpg",
    "catalog": _ASSETS / "banner_catalog.jpg",
    "profile": _ASSETS / "banner_profile.jpg",
    "reviews": _ASSETS / "banner_reviews.jpg",
    "faq": _ASSETS / "banner_faq.jpg",
}

# Кеш file_id уже загруженных баннеров (ускоряет повторную отправку).
# ВАЖНО: file_id в Telegram привязан к конкретному боту, поэтому ключ —
# (bot_id, banner): у каждого бота франшизы свой file_id для того же баннера.
_file_ids: Dict[tuple, str] = {}

CAPTION_LIMIT = 1024


def _clip(caption: str) -> str:
    return caption if len(caption) <= CAPTION_LIMIT else caption[: CAPTION_LIMIT - 1] + "…"


def _bot_id(event: Union[CallbackQuery, Message]) -> int:
    bot = getattr(event, "bot", None)
    return int(bot.id) if bot is not None else 0


def _media_source(
    banner: str, bot_id: int, override: Optional[str] = None
) -> Union[str, FSInputFile]:
    # Прямой file_id (например, баннер категории) — используем как есть.
    if override:
        return override
    key = (bot_id, banner)
    if key in _file_ids:
        return _file_ids[key]
    return FSInputFile(str(BANNERS[banner]))


def _remember(banner: str, bot_id: int, msg: Message) -> None:
    try:
        if msg.photo:
            _file_ids[(bot_id, banner)] = msg.photo[-1].file_id
    except Exception:  # noqa: BLE001
        pass


async def _send_photo(target, banner, bot_id, caption, reply_markup, photo):
    """Отправить фото-баннер с фолбэком: если file_id не принят (напр. чужой
    от другого бота) — шлём стандартный баннер с диска, иначе хотя бы текст."""
    try:
        sent = await target.answer_photo(
            _media_source(banner, bot_id, photo), caption=caption, reply_markup=reply_markup
        )
        if not photo:
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
    """Показать экран с баннером. Для callback — редактирует сообщение,
    для message — отправляет новое фото.

    ``photo`` — прямой Telegram file_id (например, свой баннер категории);
    если задан, используется вместо стандартного баннера ``banner``."""
    caption = _clip(caption)
    bot_id = _bot_id(event)

    if isinstance(event, CallbackQuery):
        msg = event.message
        media = InputMediaPhoto(
            media=_media_source(banner, bot_id, photo), caption=caption, parse_mode="HTML"
        )
        try:
            edited = await msg.edit_media(media, reply_markup=reply_markup)
            if isinstance(edited, Message) and not photo:
                _remember(banner, bot_id, edited)
            return
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message is not modified" in text:
                return
            # Чужой/битый file_id или текстовое сообщение — удалим и отправим фото заново.
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass

    target = event.message if isinstance(event, CallbackQuery) else event
    await _send_photo(target, banner, bot_id, caption, reply_markup, photo)

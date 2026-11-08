"""Очистка текстов товаров для показа на сайте.

Описания/заголовки хранятся в формате Telegram: кастомные эмодзи обёрнуты в
``<tg-emoji emoji-id="...">⭐</tg-emoji>``, а курс задаётся плейсхолдером
``{курс:1.38}``. На сайте премиум-эмодзи нет и плейсхолдеры подставлять некому,
поэтому приводим текст к обычному виду:

- ``<tg-emoji ...>⭐</tg-emoji>`` → ``⭐`` (оставляем запасной эмодзи);
- ``{курс:N}`` → N (сайт — головной магазин, наценка 0).
"""

from __future__ import annotations

import re

from bot.services.ratetext import apply_markup

_TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text):
    """Привести текст товара к виду для сайта. ``None`` → ``None``."""
    if not text:
        return text
    text = _TG_EMOJI_RE.sub(r"\1", text)
    # курс головного магазина (наценка 0): {курс:1.38} -> 1.38
    text = apply_markup(text, 0)
    return text


def clean_plain(text):
    """Как clean_text, но дополнительно убирает ВСЕ html-теги (``<b>``, ``<a>``…).

    Нужно для текста выдачи: в боте он хранится с Telegram-разметкой, а на сайте
    теги показывались бы «сырыми». Оставляем чистый текст и запасные эмодзи.
    """
    text = clean_text(text)
    if not text:
        return text
    text = _TAG_RE.sub("", text)
    # частые html-сущности → символы (на случай, если текст экранировали)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return text

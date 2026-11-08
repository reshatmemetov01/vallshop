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
# Только настоящие html-теги (чтобы текст вроде «a < b и 5>3» не ломался).
_TAG_RE = re.compile(
    r"</?(?:b|i|u|s|strong|em|a|br|p|span|div|code|pre|blockquote|ul|ol|li|h[1-6]"
    r"|tg-emoji|tg-spoiler)\b[^>]*>",
    re.IGNORECASE,
)


def clean_text(text):
    """Привести текст товара к виду для сайта. ``None`` → ``None``.

    Убирает ВСЮ Telegram-разметку: кастом-эмодзи ``<tg-emoji>⭐</tg-emoji>`` →
    ``⭐`` и любые html-теги (``<b>``, ``<i>``, ``<a>``…). На сайте форматирование
    своё, поэтому теги из текстов бота не показываем.
    """
    if not text:
        return text
    text = _TG_EMOJI_RE.sub(r"\1", text)   # <tg-emoji>⭐</tg-emoji> -> ⭐
    text = _TAG_RE.sub("", text)           # убрать остальные html-теги
    # курс головного магазина (наценка 0): {курс:1.38} -> 1.38
    text = apply_markup(text, 0)
    # частые html-сущности → символы (на случай, если текст экранировали)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return text


# Полная очистка текста выдачи — то же самое (оставлено для совместимости вызовов).
clean_plain = clean_text

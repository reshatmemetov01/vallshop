"""Подстановка курса/цены в тексте с учётом наценки магазина.

Владелец пишет базовое число внутри плейсхолдера, например:

    Курс {курс:1.38}₽ - 1 Звезда

При показе клиенту плейсхолдер заменяется на ``база × (1 + наценка%)`` этого
магазина: у головного (наценка 0) — ровно как написано, у франшизы с наценкой
10% — ×1.1. Так один базовый текст автоматически показывает корректный курс в
каждом магазине (франшиза ничего не настраивает вручную).
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

# {курс:1.38} / {КУРС: 1,38} — число с точкой или запятой, пробелы допускаются.
_RATE_RE = re.compile(r"\{курс:\s*([0-9]+(?:[.,][0-9]+)?)\s*\}", re.IGNORECASE)


def _fmt(d: Decimal) -> str:
    """2 знака после запятой, без хвостовых нулей: 1.38, 1.5, 2."""
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def apply_markup(text, markup_percent) -> str:
    """Заменить {курс:N} на N с наценкой магазина. Нет плейсхолдера → текст как есть."""
    if not text or "{курс" not in text.lower():
        return text
    mp = Decimal(markup_percent or 0)

    def _repl(m: re.Match) -> str:
        try:
            base = Decimal(m.group(1).replace(",", "."))
        except Exception:  # noqa: BLE001 — на всякий случай оставляем как есть
            return m.group(0)
        return _fmt(base * (Decimal(1) + mp / Decimal(100)))

    return _RATE_RE.sub(_repl, text)

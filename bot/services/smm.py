"""SMM-услуги soc-proof.su: расчёт цен (в рублях, за 1000), статусы, прогресс.

Деньги у поставщика — в рублях. ``rate`` — закуп за 1000. Цена клиенту:
    цена_за_1000 = закуп × (1 + наценка/100)
    итог_заказа  = цена_за_1000 × количество / 1000

Здесь только чистые функции (без БД) — их легко тестировать. Создание заказа,
фоновый опрос статусов и возвраты — в ``smm_orders.py``/поллере.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

_CENT = Decimal("0.01")
_THOUSAND = Decimal("1000")


def _dec(v, default="0") -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def sell_per_1000_rub(rate_rub, markup_pct) -> Decimal:
    """Цена продажи за 1000 (₽) = закуп × (1 + наценка/100), до копеек."""
    rate = _dec(rate_rub)
    factor = Decimal(1) + _dec(markup_pct) / Decimal(100)
    return (rate * factor).quantize(_CENT, rounding=ROUND_HALF_UP)


def order_total_rub(price_per_1000_rub, quantity: int) -> Decimal:
    """Итоговая цена заказа (₽) = цена_за_1000 × количество / 1000, до копеек."""
    total = _dec(price_per_1000_rub) * Decimal(int(quantity)) / _THOUSAND
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)


def validate_quantity(quantity, smm_min: Optional[int], smm_max: Optional[int]) -> Optional[str]:
    """Проверить количество против лимитов. None = ок, иначе текст ошибки."""
    try:
        q = int(quantity)
    except (TypeError, ValueError):
        return "Количество должно быть числом."
    lo = int(smm_min or 1)
    hi = int(smm_max or 0)
    if q < lo:
        return f"Минимум — {lo}."
    if hi and q > hi:
        return f"Максимум — {hi}."
    if q <= 0:
        return "Количество должно быть больше нуля."
    return None


def refund_amount_rub(total_paid, quantity: int, remains, *, full: bool = False) -> Decimal:
    """Сумма возврата (₽).

    full=True (отмена) → вернуть всё. Иначе (частичное выполнение) —
    пропорционально недодаче: итог × remains / количество, до копеек.
    """
    paid = _dec(total_paid)
    if full:
        return paid.quantize(_CENT, rounding=ROUND_HALF_UP)
    q = int(quantity) or 1
    rem = max(0, int(_dec(remains)))
    rem = min(rem, q)
    return (paid * Decimal(rem) / Decimal(q)).quantize(_CENT, rounding=ROUND_HALF_UP)


def progress_percent(quantity: int, remains) -> int:
    """Процент выполнения = (количество − остаток) / количество × 100 (0..100)."""
    q = int(quantity) or 1
    rem = max(0, int(_dec(remains)))
    rem = min(rem, q)
    done = q - rem
    return int(round(done * 100 / q))


# Человекочитаемые статусы для раздела «Мои заказы».
_STATUS_LABELS = {
    "pending": "⏳ В очереди",
    "in progress": "🚀 Выполняется",
    "in_progress": "🚀 Выполняется",
    "processing": "🚀 Выполняется",
    "queue": "⏳ В очереди",
    "queued": "⏳ В очереди",
    "completed": "✅ Выполнен",
    "complete": "✅ Выполнен",
    "partial": "⚠️ Частично выполнен",
    "canceled": "❌ Отменён",
    "cancelled": "❌ Отменён",
    "refunded": "↩️ Возврат",
    "fail": "❌ Ошибка",
    "failed": "❌ Ошибка",
    "error": "❌ Ошибка",
}


def status_label(raw) -> str:
    key = str(raw or "").strip().lower()
    return _STATUS_LABELS.get(key, "🚀 Выполняется")

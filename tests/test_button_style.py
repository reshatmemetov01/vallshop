"""Цвет кнопок каталога: None = синий по умолчанию, '' = без цвета, иначе — как задано."""

from bot.db.models import Product
from bot import keyboards as kb


def _styles(products):
    m = kb.products_kb(products, columns=1)
    # последняя строка — кнопка «назад», её не считаем
    out = []
    for row in m.inline_keyboard:
        for b in row:
            if b.callback_data and b.callback_data.startswith("prod:"):
                out.append(getattr(b, "style", None))
    return out


def test_product_button_styles():
    ps = [
        Product(id=1, game="g", title="A", button_style=None),      # не задан → синий
        Product(id=2, game="g", title="B", button_style=""),        # без цвета
        Product(id=3, game="g", title="C", button_style="success"),
        Product(id=4, game="g", title="D", button_style="danger"),
    ]
    assert _styles(ps) == ["primary", None, "success", "danger"]

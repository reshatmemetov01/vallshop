"""Тесты кастом-эмодзи и цветов на кнопках."""

from decimal import Decimal

from bot import keyboards as kb
from bot.keyboards import EMOJI


def _flat(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def test_persistent_menu_kb():
    m = kb.persistent_menu_kb()
    assert m.is_persistent is True
    assert m.resize_keyboard is True
    # ровно одна кнопка «Меню» с ожидаемым текстом
    assert len(m.keyboard) == 1 and len(m.keyboard[0]) == 1
    assert m.keyboard[0][0].text == kb.MENU_BUTTON_TEXT == "🏠 Меню"


def test_main_menu_one_row_with_icons_and_colors():
    markup = kb.main_menu_kb()
    # Каталог и Мой профиль — в одном (первом) ряду
    catalog, profile = markup.inline_keyboard[0]
    assert catalog.icon_custom_emoji_id == EMOJI["catalog"]
    assert catalog.style == "primary"
    assert profile.icon_custom_emoji_id == EMOJI["profile"]
    assert profile.style == "success"
    # Есть кнопка FAQ
    assert any(b.callback_data == "faq" for b in _flat(markup))


def test_profile_icons_and_orders_blue():
    btns = {b.callback_data: b for b in _flat(kb.profile_kb())}
    assert btns["balance"].icon_custom_emoji_id == EMOJI["balance"]
    assert btns["balance"].style == "danger"
    assert btns["myorders"].icon_custom_emoji_id == EMOJI["orders"]
    assert btns["myorders"].style == "primary"  # «Мои заказы» — синий
    assert btns["mytopups"].icon_custom_emoji_id == EMOJI["topups"]
    assert btns["menu"].icon_custom_emoji_id == EMOJI["back"]


def test_topup_button_icon():
    btns = {b.callback_data: b for b in _flat(kb.balance_kb())}
    assert btns["topup"].icon_custom_emoji_id == EMOJI["topup"]
    assert btns["topup"].style == "success"


def test_back_buttons_use_back_emoji():
    for markup in (kb.back_profile_kb(), kb.topup_cancel_kb()):
        btn = _flat(markup)[0]
        assert btn.icon_custom_emoji_id == EMOJI["back"]


def test_admin_button_only_for_admin():
    assert all(b.callback_data != "admin" for b in _flat(kb.main_menu_kb(False)))
    admin_btn = [b for b in _flat(kb.main_menu_kb(True)) if b.callback_data == "admin"][0]
    assert admin_btn.icon_custom_emoji_id == EMOJI["admin"]
    assert admin_btn.style == "danger"


def test_product_and_variant_icons():
    from decimal import Decimal as D

    from bot.db.models import Product, Variant

    p = Product(id=1, game="PUBG", title="PUBG Mobile Code (Global)")
    btn = _flat(kb.products_kb([p]))[0]
    assert btn.icon_custom_emoji_id == EMOJI["product"]

    v = Variant(
        id=5, product_id=1, title="325 UC",
        liog_product_id=66599, liog_variation_id=534125, cost_usd=D("4.44"),
    )
    markup = kb.variants_kb([v], {5: D("5.00")}, {5: 3}, "USDT")
    vbtn = _flat(markup)[0]
    assert vbtn.icon_custom_emoji_id == EMOJI["variant"]


def test_catalog_layout_stars_premium_paired():
    """Раскладка каталога: [Звёзды][Премиум] в паре, остальные — на всю строку."""
    from bot.db.models import Product

    products = [
        Product(id=1, game="TG_STARS", title="Телеграм Звёзды", full_width=False, sort_order=10),
        Product(id=2, game="x", title="Телеграм Премиум", full_width=False, sort_order=20),
        Product(id=3, game="Игры", title="Игры и Сервисы", full_width=True, sort_order=30),
        Product(id=4, game="STEAM_TOPUP", title="Пополнить Steam", full_width=True, sort_order=40),
        Product(id=5, game="VPN", title="VPN | VallShop", full_width=True, sort_order=50),
    ]
    rows = kb.products_kb(products, columns=2).inline_keyboard
    # строка 0 — пара Звёзды+Премиум; затем три одиночных; последняя — «Назад»
    assert [b.text for b in rows[0]] == ["Телеграм Звёзды", "Телеграм Премиум"]
    assert [b.text for b in rows[1]] == ["Игры и Сервисы"]
    assert [b.text for b in rows[2]] == ["Пополнить Steam"]
    assert [b.text for b in rows[3]] == ["VPN | VallShop"]
    assert len(rows[4]) == 1  # кнопка «Назад»

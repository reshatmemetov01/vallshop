"""Клавиатура поста рассылки: разделы (callback) и ссылки (url), цвет, эмодзи."""

from bot import keyboards as kb


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_empty_buttons_is_none():
    assert kb.broadcast_post_kb([]) is None
    assert kb.broadcast_post_kb(None) is None


def test_section_and_url_buttons():
    buttons = [
        {"label": "Каталог", "kind": "cb", "target": "catalog",
         "style": "primary", "icon": "123"},
        {"label": "Канал", "kind": "url", "target": "@mychan",
         "style": "success", "icon": None},
    ]
    m = kb.broadcast_post_kb(buttons)
    btns = _flat(m)
    assert btns[0].callback_data == "catalog"
    assert getattr(btns[0], "style", None) == "primary"
    assert getattr(btns[0], "icon_custom_emoji_id", None) == "123"
    # @username нормализуется в t.me-ссылку
    assert btns[1].url == "https://t.me/mychan"
    assert btns[1].callback_data is None
    assert getattr(btns[1], "style", None) == "success"


def test_one_per_row():
    buttons = [
        {"label": "A", "kind": "cb", "target": "catalog"},
        {"label": "B", "kind": "cb", "target": "profile"},
    ]
    m = kb.broadcast_post_kb(buttons)
    assert all(len(row) == 1 for row in m.inline_keyboard)
    assert len(m.inline_keyboard) == 2


def test_bad_url_button_skipped():
    # Невалидная цель url-кнопки не роняет всю клавиатуру, а пропускается.
    buttons = [
        {"label": "Нет url", "kind": "url", "target": ""},
        {"label": "Каталог", "kind": "cb", "target": "catalog"},
    ]
    m = kb.broadcast_post_kb(buttons)
    btns = _flat(m)
    assert len(btns) == 1
    assert btns[0].callback_data == "catalog"

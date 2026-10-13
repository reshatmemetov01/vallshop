"""Франшиза FAQ: кнопки документов (ссылка или текст в боте) + поддержка."""

import pytest

from bot.db import Database
from bot import keyboards as kb
from bot.services import menu as menu_service
from bot.handlers.user import _doc_value, _is_url


@pytest.fixture
async def db():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    await menu_service.load(database)      # чистый кэш оформления
    yield database
    await database.dispose()


def test_doc_value_url_vs_text():
    assert _is_url("https://x.ru/offer") is True
    assert _is_url("Текст оферты") is False
    # ссылка — как есть; текст — колбэк-документ; пусто — None
    assert _doc_value("https://x.ru", "offer") == "https://x.ru"
    assert _doc_value("Полный текст оферты", "offer") == "cb:faqdoc:offer"
    assert _doc_value(None, "offer") is None


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_faq_kb_franchise_doc_buttons(db):
    # оферта текстом → кнопка-колбэк; приватность ссылкой → url; соглашение не задано → нет
    m = kb.faq_kb(
        offer_url="cb:faqdoc:offer", agreement_url=None, privacy_url="https://x.ru/priv",
        merchant_id=1, support_url="https://t.me/shop",
    )
    btns = _buttons(m)
    offer = [b for b in btns if b.callback_data == "faqdoc:offer"]
    priv = [b for b in btns if b.url == "https://x.ru/priv"]
    sup = [b for b in btns if b.url == "https://t.me/shop"]
    assert offer and priv and sup          # оферта-колбэк, приватность-url, поддержка-url
    # соглашение не задано → его кнопки нет
    assert not any((b.url or "").endswith("agreement") for b in btns)


@pytest.mark.asyncio
async def test_faq_kb_franchise_empty_hides_buttons(db):
    # ничего не задано → остаётся только «Меню» (документов/поддержки нет)
    m = kb.faq_kb(merchant_id=1)
    btns = _buttons(m)
    assert all(not b.callback_data or b.callback_data == "menu" for b in btns)
    assert all(b.url is None for b in btns)

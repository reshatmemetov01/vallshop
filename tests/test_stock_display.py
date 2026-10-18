"""Наличие у поставщика FazerCard: отсутствующий оффер → нет; красная кнопка."""

import pytest

from bot.db.models import Variant
from bot.services import catalog as catalog_service
from bot import keyboards as kb


class _FakeFzr:
    """offers_for возвращает фиксированный список офферов категории."""
    def __init__(self, offers):
        self._offers = offers

    def offers_for(self, kind, cat_id):
        return self._offers


def _variant(vid, offer_id):
    return Variant(id=vid, product_id=1, title=f"v{vid}", source="fazercard",
                   fzr_kind="giftcard", fzr_a="cat1", fzr_b=offer_id,
                   liog_product_id=0, liog_variation_id=-1)


@pytest.mark.asyncio
async def test_absent_offer_is_out_of_stock():
    # Поставщик отдаёт только оффер "A" (stock 5) и "B" (без поля stock).
    fzr = _FakeFzr([{"id": "A", "stock": 5}, {"id": "B"}])
    variants = [_variant(1, "A"), _variant(2, "B"), _variant(3, "C")]  # C отсутствует
    res = await catalog_service.fazercard_stock(fzr, variants)
    assert res[1] == 5        # есть, сток известен
    assert res[2] is None     # есть, сток неизвестен (под заказ)
    assert res[3] == 0        # ОТСУТСТВУЕТ у поставщика → нет в наличии


def test_variants_kb_red_button_when_out():
    v_in = _variant(1, "A")
    v_out = _variant(2, "C")
    prices = {1: "100 ₽", 2: "200 ₽"}
    stock = {1: 5, 2: 0}      # 2 — нет
    m = kb.variants_kb([v_in, v_out], prices, stock)
    btns = {b.callback_data: b for row in m.inline_keyboard for b in row}
    out_btn = btns["var:2"]
    in_btn = btns["var:1"]
    assert "нет в наличии" in out_btn.text.lower()
    # красная кнопка у отсутствующего (style прокидывается как кастом-поле)
    assert getattr(out_btn, "style", None) == "danger"
    assert getattr(in_btn, "style", None) in (None, "")


def test_out_of_stock_kb_has_no_buy():
    m = kb.out_of_stock_kb("prod:1")
    cbs = [b.callback_data for row in m.inline_keyboard for b in row]
    assert not any((c or "").startswith("buy:") for c in cbs)

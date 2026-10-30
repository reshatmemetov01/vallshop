"""Тест чистки названий номиналов Google Play (scripts/clean_gp_titles)."""

from scripts.clean_gp_titles import _clean


def test_strip_google_play_prefix():
    assert _clean("Google Play (US) · 5 USD") == "5 USD"
    assert _clean("Google Play (TR) · 25 TRY") == "25 TRY"
    assert _clean("Google Play (US) 100 USD") == "100 USD"      # без разделителя
    assert _clean("Google Play · 50 EUR") == "50 EUR"            # без страны в скобках


def test_leaves_others_untouched():
    assert _clean("Steam · 10 USD") == "Steam · 10 USD"
    assert _clean("5 USD") == "5 USD"                            # уже чистое


def test_never_blank():
    # если после удаления ничего не осталось — оставляем как было
    assert _clean("Google Play (US)") == "Google Play (US)"

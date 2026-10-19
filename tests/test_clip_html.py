"""HTML-безопасная обрезка подписи (_clip): не рвёт теги, не превышает лимит."""

from bot.ui import _clip, CAPTION_LIMIT


def test_short_caption_unchanged():
    assert _clip("привет <b>мир</b>") == "привет <b>мир</b>"


def test_closes_code_cut_in_middle():
    cap = "A" * (CAPTION_LIMIT - 20) + " коды: <code>XXXXXXXXXXXXXXXX</code> конец"
    out = _clip(cap)
    assert len(out) <= CAPTION_LIMIT
    assert out.count("<code>") == out.count("</code>")  # тег дозакрыт


def test_drops_dangling_tag_fragment():
    cap = "B" * (CAPTION_LIMIT - 3) + "<code>zzz"
    out = _clip(cap)
    assert len(out) <= CAPTION_LIMIT
    # оборванный фрагмент "<cod…" выброшен, незакрытых тегов нет
    assert out.count("<code>") == out.count("</code>") == 0


def test_nested_tags_balanced():
    cap = "C" * (CAPTION_LIMIT - 10) + "<b><code>yy</code></b>"
    out = _clip(cap)
    assert len(out) <= CAPTION_LIMIT
    assert out.count("<b>") == out.count("</b>")
    assert out.count("<code>") == out.count("</code>")


def test_huge_single_tag_fits_limit():
    cap = "<code>" + "D" * (CAPTION_LIMIT * 2)
    out = _clip(cap)
    assert len(out) <= CAPTION_LIMIT
    assert out.count("<code>") == out.count("</code>")

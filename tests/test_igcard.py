"""Story-card renderer: geometry, text cleanup, and the truncation floor."""
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from rbga.bot import igcard


def test_render_card_is_story_sized_png():
    png = igcard.render_card("Games night this Friday!")
    img = Image.open(BytesIO(png))
    assert img.format == "PNG"
    assert img.size == (igcard.WIDTH, igcard.HEIGHT)


def test_render_card_rejects_empty_text():
    with pytest.raises(ValueError):
        igcard.render_card("   \n ")


def test_clean_strips_markdown_and_custom_emoji():
    raw = "**Games Night!** __this__ `Friday` <:rbga:12345> <a:party:99>"
    assert igcard.clean_announcement(raw) == "Games Night! this Friday"


def test_clean_strips_unicode_emoji():
    assert igcard.clean_announcement("Pizza 🍕 provided 🎲🎉") == "Pizza provided"


def test_clean_collapses_blank_line_runs():
    assert igcard.clean_announcement("a\n\n\n\n\nb") == "a\n\nb"


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (1, 1)))


def test_fit_short_text_uses_max_font_untruncated():
    font, lines, truncated = igcard._fit(_draw(), "Hi all")
    assert font.size == igcard.MAX_FONT
    assert not truncated
    assert lines == ["Hi all"]


def test_fit_long_text_hits_floor_and_truncates_with_tail():
    text = "every member should come along to the annual general meeting " * 60
    font, lines, truncated = igcard._fit(_draw(), text.strip())
    assert truncated
    assert font.size == igcard.MIN_FONT
    assert lines[-1] == igcard.TAIL
    # The truncated block (tail included) still fits the text band.
    band = igcard.TEXT_BOTTOM - igcard.TEXT_TOP
    assert len(lines) * igcard.MIN_FONT * igcard.LINE_SPACING <= band


def test_wrap_hard_breaks_overwide_words():
    # One giant unbroken "word" (e.g. a URL) must still wrap, not overflow.
    long_word = "x" * 500
    lines = igcard._wrap(_draw(), long_word, igcard._font(igcard.MIN_FONT), 300)
    assert len(lines) > 1
    assert "".join(lines) == long_word

"""Render an announcement as an Instagram-story card (1080x1920 PNG).

Instagram's publishing API only accepts a finished image, so the "create
mode"-style text story (coloured background, text on top) is drawn here with
Pillow. Pure and synchronous: callers on the event loop must run render_card
in a worker thread (it's CPU work).

Text fitting: the font starts at MAX_FONT and shrinks until the wrapped text
fits the band the Instagram UI doesn't cover; below MIN_FONT it stops shrinking
(legibility floor) and truncates with a "full announcement on our Discord"
tail line instead.
"""
import re
from io import BytesIO
from pathlib import Path

from discord.utils import remove_markdown
from PIL import Image, ImageDraw, ImageFont

# Story canvas; margins keep text clear of the IG overlays (profile chip up
# top, reply bar down the bottom).
WIDTH, HEIGHT = 1080, 1920
MARGIN_X = 110
TEXT_TOP, TEXT_BOTTOM = 320, 1540  # vertical band the announcement may occupy

# Background gradient (top -> bottom) and text colours. Change these to
# re-brand the cards; nothing else encodes the look.
BG_TOP = (48, 18, 110)  # deep violet
BG_BOTTOM = (16, 46, 104)  # dark blue
TEXT_COLOUR = (255, 255, 255)
FOOTER_COLOUR = (205, 205, 228)
FOOTER = "RMIT Board Game Association"
FOOTER_SIZE = 38
TAIL = "…full announcement on our Discord"

MAX_FONT, MIN_FONT, FONT_STEP = 76, 44, 4
LINE_SPACING = 1.3  # multiple of the font size

_ASSETS = Path(__file__).resolve().parents[1] / "assets"

# Discord custom emoji (<:name:id> / animated <a:name:id>) render as raw codes
# in plain text, and unicode emoji render as tofu boxes in a normal TTF, so
# both are stripped rather than mangled.
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_UNICODE_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji & symbol blocks
    "☀-➿"  # misc symbols + dingbats
    "←-⇿"  # arrows
    "️‍"  # variation selector-16 + zero-width joiner
    "]+"
)


def clean_announcement(text: str) -> str:
    """Discord text -> plain card text: drop custom/unicode emoji, strip
    markdown, collapse blank-line runs. Mentions should already be resolved
    (pass Message.clean_content, not Message.content)."""
    text = _CUSTOM_EMOJI.sub("", text)
    text = _UNICODE_EMOJI.sub("", text)
    text = remove_markdown(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse intra-line whitespace left behind by stripped emoji.
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    return text.strip()


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont:
    name = "Poppins-Bold.ttf" if bold else "Poppins-Regular.ttf"
    try:
        return ImageFont.truetype(str(_ASSETS / name), size)
    except OSError:  # font not shipped (odd install); degrade, don't crash
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Greedy word wrap by measured pixel width; words wider than the column
    are hard-broken so one long URL can't blow the layout."""
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        current = ""
        for word in para.split(" "):
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # Hard-break an over-wide word by characters.
            while draw.textlength(word, font=font) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str) -> tuple[ImageFont.FreeTypeFont, list[str], bool]:
    """Pick the largest font size whose wrapped text fits the band; at the
    MIN_FONT floor, truncate and add the TAIL line. Returns (font, lines,
    truncated)."""
    max_width = WIDTH - 2 * MARGIN_X
    band_height = TEXT_BOTTOM - TEXT_TOP
    for size in range(MAX_FONT, MIN_FONT - 1, -FONT_STEP):
        font = _font(size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) * size * LINE_SPACING <= band_height:
            return font, lines, False
    font = _font(MIN_FONT)
    lines = _wrap(draw, text, font, max_width)
    max_lines = int(band_height / (MIN_FONT * LINE_SPACING)) - 1  # reserve the tail line
    return font, lines[:max_lines] + [TAIL], True


def render_card(text: str) -> bytes:
    """The announcement as story-card PNG bytes. `text` should already be
    cleaned (clean_announcement); raises ValueError if nothing renderable
    is left."""
    if not text.strip():
        raise ValueError("no text to render")

    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # vertical gradient, one row at a time
        t = y / (HEIGHT - 1)
        colour = tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM))
        draw.line([(0, y), (WIDTH, y)], fill=colour)

    font, lines, _ = _fit(draw, text)
    line_height = font.size * LINE_SPACING
    block_height = len(lines) * line_height
    y = TEXT_TOP + (TEXT_BOTTOM - TEXT_TOP - block_height) / 2  # centre in the band
    for line in lines:
        draw.text((WIDTH / 2, y), line, font=font, fill=TEXT_COLOUR, anchor="ma")
        y += line_height

    draw.text(
        (WIDTH / 2, HEIGHT - 130),
        FOOTER,
        font=_font(FOOTER_SIZE, bold=False),
        fill=FOOTER_COLOUR,
        anchor="ma",
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

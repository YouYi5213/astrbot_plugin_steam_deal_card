"""Pillow based renderers that turn Steam data into shareable card images."""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .models import DealItem, GameCard, LowestPrice, PlayerCount, PriceInfo

# Palette shared by both card styles.
BG = (24, 33, 47)
PANEL = (33, 46, 63)
PANEL_ALT = (39, 54, 73)
TEXT = (238, 244, 250)
TEXT_DIM = (146, 168, 191)
TEXT_FAINT = (108, 130, 153)
ACCENT = (116, 186, 255)
DISCOUNT = (108, 214, 146)
DEAL_BG = (52, 132, 88)
WARN = (255, 190, 118)
DIVIDER = (52, 70, 92)

CARD_WIDTH = 940
PADDING = 28

# Sentence marks that should never be left dangling at the end of a wrapped
# line, which reads as a rendering bug rather than as truncation.
_TRAILING_MARKS = "。，、；：！？,.;:!?）)】」』 \t"

# Player counts are a "right now" figure, so the card stamps it in a local
# clock rather than UTC. China has used a single fixed +08:00 offset since 1991
# and observes no DST, so the fallback below is exact, not an approximation.
_DISPLAY_TIMEZONE = "Asia/Shanghai"
_CST = timezone(timedelta(hours=8), "CST")


def _display_timezone() -> tzinfo:
    """Resolve the timezone used on the player count cards.

    ``zoneinfo`` needs the system tz database, which slim container images
    often omit. Falling back to the fixed offset keeps the card rendering
    instead of raising on those images.

    Returns:
        A tzinfo for China Standard Time.
    """
    try:
        return ZoneInfo(_DISPLAY_TIMEZONE)
    except Exception:  # noqa: BLE001 - any failure means the tz db is unusable
        return _CST


# Font files are searched in order; the first usable one wins.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_BUNDLED_FONT = Path(__file__).with_name("assets") / "fonts" / "NotoSansSC-Regular.otf"

_resolved_font: str | None = None
_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _resolve_font_path() -> str | None:
    """Find a font file that can render Chinese text.

    Returns:
        Path to a usable font, or None when only the PIL bitmap default exists.
    """
    global _resolved_font
    if _resolved_font is not None:
        return _resolved_font or None

    candidates = [str(_BUNDLED_FONT), *_FONT_CANDIDATES]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            ImageFont.truetype(path, 16)
        except OSError:
            continue
        _resolved_font = path
        return path
    _resolved_font = ""
    return None


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return a cached font at the requested size.

    Args:
        size: Font size in pixels.

    Returns:
        A Pillow font object.
    """
    key = (size, False)
    if key in _font_cache:
        return _font_cache[key]
    path = _resolve_font_path()
    if path:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except OSError:
            pass
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    """Measure rendered text width.

    Args:
        draw: Draw context used for measurement.
        text: Text to measure.
        font: Font to measure with.

    Returns:
        The width in pixels.
    """
    return draw.textlength(text, font=font)


def _truncate(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: float,
) -> str:
    """Shorten text with an ellipsis until it fits the given width.

    Args:
        draw: Draw context used for measurement.
        text: Text to shorten.
        font: Font to measure with.
        max_width: Available width in pixels.

    Returns:
        Text that fits, ending in an ellipsis when it had to be cut.
    """
    if max_width <= 0 or not text:
        return ""
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(draw, text[:mid] + ellipsis, font) <= max_width:
            low = mid
        else:
            high = mid - 1
    if not low:
        return ""
    cut = text[:low]
    # Prefer cutting on a word boundary: "should at …" reads worse than
    # "should …". CJK has no spaces, so this leaves those cuts untouched.
    if not text[low].isspace() and " " in cut.rstrip():
        head = cut.rstrip().rsplit(" ", 1)[0]
        if head and _text_width(draw, head + ellipsis, font) <= max_width:
            cut = head
    return cut.rstrip(_TRAILING_MARKS) + ellipsis


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: float,
    max_lines: int = 2,
) -> list[str]:
    """Wrap text into at most ``max_lines`` lines.

    CJK text has no spaces, so it is broken character by character; Latin words
    are kept whole where possible.

    Args:
        draw: Draw context used for measurement.
        text: Text to wrap.
        font: Font to measure with.
        max_width: Available width in pixels.
        max_lines: Maximum number of lines to produce.

    Returns:
        The wrapped lines, with the final line ellipsized when truncated.
    """
    if not text:
        return []
    lines: list[str] = []
    current = ""
    # Track how much of the source has been consumed. Stripping tokens for
    # display makes the rendered length differ from the source length, so the
    # truncation tail cannot be derived from the lines themselves.
    consumed = 0
    truncated = False
    # Latin text is split into words (with their trailing space) so a break
    # lands between words; CJK has no spaces and is split per character.
    for token in _wrap_tokens(text):
        candidate = current + token
        if _text_width(draw, candidate, font) <= max_width or not current:
            current = candidate
            consumed += len(token)
            continue
        lines.append(current.strip())
        stripped = token.lstrip()
        consumed += len(token) - len(stripped)
        current = stripped
        consumed += len(stripped)
        if len(lines) == max_lines:
            truncated = True
            break
    if current.strip() and len(lines) < max_lines:
        lines.append(current.strip())

    if truncated and consumed < len(text):
        remainder = text[consumed:]
        # The cut can fall on a space, which would otherwise glue the next word
        # onto the previous one.
        joiner = "" if not remainder or remainder[0].isspace() else " "
        lines[-1] = _truncate(draw, f"{lines[-1]}{joiner}{remainder}".strip(), font, max_width)
    # Stop a line from ending on a dangling sentence mark, which happens when a
    # wrap boundary lands right after punctuation.
    if lines:
        lines[-1] = lines[-1].rstrip(_TRAILING_MARKS)
    return [line for line in lines[:max_lines] if line]


def _wrap_tokens(text: str) -> list[str]:
    """Split text into wrap units, keeping Latin words whole.

    CJK is written without spaces, so those characters become one unit each.
    Runs of Latin letters, digits and inner punctuation stay together with any
    following space, which stops a line from breaking mid-word.

    Args:
        text: Text to split.

    Returns:
        The tokens, in order.
    """
    tokens: list[str] = []
    word = ""
    for char in text:
        # A space ends a Latin word; anything else accumulates.
        if char == " ":
            if word:
                tokens.append(word)
                word = ""
            tokens.append(" ")
        elif char.isascii() and (char.isalnum() or char in "'-.:_+&/"):
            word += char
        else:
            if word:
                tokens.append(word)
                word = ""
            tokens.append(char)
    if word:
        tokens.append(word)
    # Absorb each space into the preceding token so wrapping keeps the gap
    # only between words, never at the start of a line.
    merged: list[str] = []
    for token in tokens:
        if token == " " and merged:
            merged[-1] += " "
        else:
            merged.append(token)
    return merged


def _rounded_image(image: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    """Resize an image to fill a size and round its corners.

    Args:
        image: Source image.
        size: Target ``(width, height)``.
        radius: Corner radius in pixels.

    Returns:
        An RGBA image of exactly ``size`` with rounded corners.
    """
    target_w, target_h = size
    source = image.convert("RGBA")
    scale = max(target_w / source.width, target_h / source.height)
    resized = source.resize(
        (max(int(source.width * scale), 1), max(int(source.height * scale), 1)),
        Image.LANCZOS,
    )
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    cropped = resized.crop((left, top, left + target_w, top + target_h))

    mask = Image.new("L", (target_w, target_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, target_w - 1, target_h - 1), radius, fill=255)
    cropped.putalpha(mask)
    return cropped


def _placeholder(size: tuple[int, int], radius: int) -> Image.Image:
    """Build a neutral placeholder for a missing capsule image.

    Args:
        size: Target ``(width, height)``.
        radius: Corner radius in pixels.

    Returns:
        An RGBA placeholder image.
    """
    target_w, target_h = size
    image = Image.new("RGBA", size, PANEL_ALT)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, target_w - 1, target_h - 1), radius, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)
    label = _font(20)
    text = "暂无图片"
    draw.text(
        (target_w / 2, target_h / 2),
        text,
        font=label,
        fill=TEXT_FAINT,
        anchor="mm",
    )
    return image


def _load_capsule(data: bytes | None, size: tuple[int, int], radius: int = 12) -> Image.Image:
    """Turn downloaded bytes into a rounded capsule image.

    Args:
        data: Raw image bytes, or None when the download failed.
        size: Target ``(width, height)``.
        radius: Corner radius in pixels.

    Returns:
        A rounded capsule image, falling back to a placeholder.
    """
    if not data:
        return _placeholder(size, radius)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return _rounded_image(image, size, radius)
    except Exception:  # noqa: BLE001 - a broken image must not break the card
        return _placeholder(size, radius)


def _draw_price(
    draw: ImageDraw.ImageDraw,
    price: PriceInfo,
    x: float,
    y: float,
    *,
    current_size: int = 44,
    show_original: bool = True,
) -> float:
    """Draw a price line with an optional struck-through original price.

    Args:
        draw: Draw context.
        price: Price data to render.
        x: Left edge in pixels.
        y: Baseline top in pixels.
        current_size: Font size for the current price.
        show_original: Whether to draw the original price and discount badge.

    Returns:
        The x offset just past the rendered content.
    """
    current_font = _font(current_size)
    draw.text((x, y), price.formatted_current, font=current_font, fill=ACCENT)
    cursor = x + _text_width(draw, price.formatted_current, current_font)

    if not show_original or not price.is_discounted:
        return cursor

    small = _font(20)
    original = price.formatted_original
    original_x = cursor + 14
    original_y = y + current_size - 26
    draw.text((original_x, original_y), original, font=small, fill=TEXT_FAINT)
    width = _text_width(draw, original, small)
    strike_y = original_y + 10
    draw.line((original_x, strike_y, original_x + width, strike_y), fill=TEXT_FAINT, width=2)

    badge_x = original_x + width + 14
    badge_text = f"-{price.discount_percent}%"
    badge_font = _font(20)
    text_w = _text_width(draw, badge_text, badge_font)
    badge_w = text_w + 20
    badge_h = 30
    badge_y = y + current_size - 36
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
        radius=8,
        fill=DEAL_BG,
    )
    draw.text(
        (badge_x + badge_w / 2, badge_y + badge_h / 2),
        badge_text,
        font=badge_font,
        fill=(255, 255, 255),
        anchor="mm",
    )
    return badge_x + badge_w


def _draw_lowest(
    draw: ImageDraw.ImageDraw,
    lowest: LowestPrice | None,
    x: float,
    y: float,
    current: PriceInfo | None = None,
    right_edge: float | None = None,
) -> None:
    """Draw the all time lowest price line.

    Args:
        draw: Draw context.
        lowest: Lowest price data, or None when unavailable.
        x: Left edge in pixels.
        y: Top edge in pixels.
        current: Current price, used to add a comparison note.
        right_edge: When given, the distance from the current price to the
            lowest price is drawn flush against this right edge.
    """
    font = _font(22)
    if lowest is None:
        draw.text((x, y), "史低：暂无记录", font=font, fill=TEXT_FAINT)
        return

    text = f"史低 {_money(lowest.value, lowest.currency)}"
    draw.text((x, y), text, font=font, fill=DISCOUNT)
    cursor = x + _text_width(draw, text, font)

    details = []
    if lowest.recorded_on:
        details.append(lowest.recorded_on)
    if lowest.discount_percent:
        details.append(f"-{lowest.discount_percent}%")

    # The gap is what the user actually wants to know: without it, "close to
    # the low" and "twice the low" read the same. It gets its own space on the
    # right rather than being appended into a line that may already be long.
    gap_text = ""
    if right_edge is not None and current is not None and current.current_value is not None:
        if current.current_value <= lowest.value:
            gap_text = "当前已是史低"
        else:
            gap = current.current_value - lowest.value
            gap_text = f"距史低还差 {_money(gap, current.currency or lowest.currency)}"

    if gap_text:
        gap_font = _font(22)
        gap_w = _text_width(draw, gap_text, gap_font)
        # Only draw the badge when it cannot collide with the left-hand text.
        if cursor + 20 + gap_w <= right_edge:
            draw.text(
                (right_edge, y),
                gap_text,
                font=gap_font,
                fill=DISCOUNT if gap_text == "当前已是史低" else WARN,
                anchor="ra",
            )
        else:
            details.append(gap_text)
    if details:
        draw.text((cursor + 10, y), "（" + "，".join(details) + "）", font=font, fill=TEXT_DIM)


def _money(value, currency: str) -> str:
    """Format a Decimal amount with a currency symbol.

    Args:
        value: Amount to format.
        currency: ISO currency code.

    Returns:
        A display string such as ``¥18`` or ``5 USD``.
    """
    symbols = {"CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
    amount = f"{value:.2f}".rstrip("0").rstrip(".")
    symbol = symbols.get((currency or "").upper())
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}".strip()


def _format_end(
    discount_end: datetime | None,
    now: datetime | None = None,
    compact: bool = False,
) -> str:
    """Describe when a discount ends, including days remaining.

    Args:
        discount_end: Discount end time in UTC, or None.
        now: Reference time, defaulting to the current UTC time.
        compact: Drop the year when it matches the reference year, which keeps
            the text short enough for the dense deals list.

    Returns:
        A display string, or an empty string when there is no end time.
    """
    if discount_end is None:
        return ""
    moment = now or datetime.now(timezone.utc)
    remaining = (discount_end - moment).days
    if compact and discount_end.year == moment.year:
        stamp = discount_end.strftime("%m-%d")
    else:
        stamp = discount_end.strftime("%Y-%m-%d")
    if remaining < 0:
        return f"折扣已结束（{stamp}）"
    if remaining == 0:
        return f"折扣今天结束（{stamp}）"
    return f"折扣 {stamp} 结束（剩 {remaining} 天）"


def render_game_card(card: GameCard, capsule: bytes | None = None) -> bytes:
    """Render a single game detail card.

    Args:
        card: Game data to render.
        capsule: Raw capsule image bytes, or None.

    Returns:
        PNG image bytes.
    """
    measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    image_size = (330, 189)
    right_x = PADDING + image_size[0] + 24
    right_w = CARD_WIDTH - PADDING - right_x

    title_font = _font(34)
    title_lines = _wrap(measure, card.name, title_font, right_w, max_lines=2)

    meta_font = _font(20)
    top_block_h = max(image_size[1], len(title_lines) * 42 + 78)

    # The description is already returned by the store query; showing it makes
    # the card answer "what is this game" rather than only "what does it cost".
    desc_font = _font(21)
    desc_lines = (
        _wrap(measure, card.short_description, desc_font, CARD_WIDTH - PADDING * 2, max_lines=3)
        if card.short_description
        else []
    )

    price_block_y = PADDING + top_block_h + 24
    has_end = bool(card.price and card.price.discount_end)
    desc_h = len(desc_lines) * 30 + 18 if desc_lines else 0
    # No footer row: the store links travel as text alongside the image, where
    # they can actually be tapped.
    height = price_block_y + 62 + 34 + (30 if has_end else 0) + desc_h + PADDING

    image = Image.new("RGB", (CARD_WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    # Header panel behind the capsule and title.
    draw.rounded_rectangle(
        (PADDING - 12, PADDING - 12, CARD_WIDTH - PADDING + 12, PADDING + top_block_h + 12),
        radius=16,
        fill=PANEL,
    )

    capsule_image = _load_capsule(capsule, image_size)
    image.paste(capsule_image, (PADDING, PADDING), capsule_image)

    cursor_y = PADDING
    for line in title_lines:
        draw.text((right_x, cursor_y), line, font=title_font, fill=TEXT)
        cursor_y += 42

    if card.reviews:
        review = card.reviews
        draw.text(
            (right_x, cursor_y + 4),
            f"{review.label} · {review.percent_positive}% 好评 · {review.review_count:,} 篇评测",
            font=meta_font,
            fill=DISCOUNT,
        )
    else:
        draw.text((right_x, cursor_y + 4), "评价：暂无", font=meta_font, fill=TEXT_FAINT)
    cursor_y += 34

    meta_bits = []
    if card.release_date:
        meta_bits.append(f"发行 {card.release_date}")
    if card.developers:
        meta_bits.append(" / ".join(card.developers[:2]))
    if card.is_free:
        meta_bits.append("免费游玩")
    if meta_bits:
        draw.text(
            (right_x, cursor_y + 4),
            _truncate(measure, " · ".join(meta_bits), meta_font, right_w),
            font=meta_font,
            fill=TEXT_DIM,
        )

    draw.line(
        (PADDING, price_block_y - 14, CARD_WIDTH - PADDING, price_block_y - 14),
        fill=DIVIDER,
        width=1,
    )

    if card.price:
        _draw_price(draw, card.price, PADDING, price_block_y)
    elif card.is_free:
        draw.text((PADDING, price_block_y), "免费游玩", font=_font(44), fill=ACCENT)
    else:
        draw.text((PADDING, price_block_y), "暂无价格", font=_font(34), fill=TEXT_FAINT)

    lowest_y = price_block_y + 62
    _draw_lowest(
        draw,
        card.lowest,
        PADDING,
        lowest_y,
        card.price,
        right_edge=CARD_WIDTH - PADDING,
    )

    if has_end and card.price:
        draw.text(
            (PADDING, lowest_y + 34),
            _format_end(card.price.discount_end),
            font=_font(22),
            fill=WARN,
        )

    # Description sits above the footer, separated by a divider so it reads as
    # supporting detail rather than part of the price block.
    if desc_lines:
        desc_y = lowest_y + 34 + (30 if has_end else 0) + 20
        draw.line((PADDING, desc_y - 12, CARD_WIDTH - PADDING, desc_y - 12), fill=DIVIDER, width=1)
        for index, line in enumerate(desc_lines):
            draw.text(
                (PADDING, desc_y + index * 30),
                line,
                font=desc_font,
                fill=TEXT_DIM,
            )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_deals_card(
    deals: list[DealItem],
    capsules: dict[int, bytes] | None = None,
    title: str = "Steam 特惠",
    now: datetime | None = None,
) -> bytes:
    """Render the current Steam specials as a single list image.

    Args:
        deals: Discounted games to render.
        capsules: Mapping of appid to raw capsule image bytes.
        title: Heading shown at the top of the card.
        now: Reference time for remaining-day calculations.

    Returns:
        PNG image bytes.
    """
    return render_ranking_card(
        deals,
        capsules=capsules,
        title=title,
        subtitle=f"共 {len(deals)} 款折扣游戏 · 数据来自 Steam 商店",
        now=now,
        show_lowest=True,
        show_discount=True,
        show_end=True,
    )


def render_ranking_card(
    items: list[DealItem],
    capsules: dict[int, bytes] | None = None,
    title: str = "Steam",
    subtitle: str = "",
    now: datetime | None = None,
    show_lowest: bool = False,
    show_discount: bool = False,
    show_end: bool = False,
    note: str = "",
) -> bytes:
    """Render a list of games as a single ranking image.

    Shared by the specials, popular-new and upcoming listings, which differ
    only in which fields are meaningful for them.

    Args:
        items: Games to render, in the order they should appear.
        capsules: Mapping of appid to raw capsule image bytes.
        title: Heading shown at the top of the card.
        subtitle: Small right-aligned line beside the heading.
        now: Reference time for remaining-day calculations.
        show_lowest: Draw the all time lowest price line.
        show_discount: Draw the discount badge.
        show_end: Draw the discount end date.
        note: Optional explanatory line under the heading.

    Returns:
        PNG image bytes.
    """
    capsules = capsules or {}
    row_height = 132
    header_height = 96 + (34 if note else 0)
    count = len(items)
    height = header_height + count * row_height + PADDING

    image = Image.new("RGB", (CARD_WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.text((PADDING, 30), title, font=_font(38), fill=TEXT)
    if subtitle:
        draw.text(
            (CARD_WIDTH - PADDING, 44),
            subtitle,
            font=_font(20),
            fill=TEXT_FAINT,
            anchor="ra",
        )
    if note:
        draw.text((PADDING, 78), note, font=_font(19), fill=TEXT_FAINT)

    thumb_size = (196, 112)
    name_font = _font(26)
    info_font = _font(20)

    for index, deal in enumerate(items):
        top = header_height + index * row_height
        draw.rounded_rectangle(
            (PADDING - 10, top, CARD_WIDTH - PADDING + 10, top + row_height - 12),
            radius=14,
            fill=PANEL if index % 2 == 0 else PANEL_ALT,
        )

        thumb = _load_capsule(capsules.get(deal.appid), thumb_size, radius=10)
        image.paste(thumb, (PADDING, top + 10), thumb)

        text_x = PADDING + thumb_size[0] + 20

        # Reserve exactly as much room as the right-hand price column needs, so
        # long game names use every remaining pixel instead of a guessed budget.
        price_font = _font(32)
        price_text = deal.price.formatted_current
        price_w = _text_width(draw, price_text, price_font)
        badge_font = _font(20)
        badge_text = (
            f"-{deal.price.discount_percent}%" if show_discount and deal.price.is_discounted else ""
        )
        badge_w = _text_width(draw, badge_text, badge_font) + 18 if badge_text else 0.0
        reserved = max(price_w, badge_w) + 24
        name_width = CARD_WIDTH - PADDING - 10 - text_x - reserved

        name = _truncate(draw, deal.name, name_font, name_width)
        draw.text((text_x, top + 18), name, font=name_font, fill=TEXT)

        if deal.reviews and deal.reviews.review_count:
            review_text = (
                f"{deal.reviews.label} · {deal.reviews.percent_positive}% 好评 · "
                f"{deal.reviews.review_count:,} 篇"
            )
            draw.text(
                (text_x, top + 54),
                _truncate(draw, review_text, info_font, name_width),
                font=info_font,
                fill=DISCOUNT,
            )
        elif deal.reviews:
            # Steam reports an empty summary for something like an unreleased
            # game; printing "0% 好评 · 0 篇" would read as a bad score.
            draw.text((text_x, top + 54), "暂无用户评测", font=info_font, fill=TEXT_FAINT)

        # The bottom row carries whichever context lines apply to this listing.
        bottom_x = text_x
        if show_lowest:
            lowest_text = (
                f"史低 {_money(deal.lowest.value, deal.lowest.currency)}"
                if deal.lowest
                else "史低 暂无"
            )
            draw.text((bottom_x, top + 84), lowest_text, font=info_font, fill=TEXT_DIM)
            bottom_x += _text_width(draw, lowest_text, info_font) + 24

        if show_end:
            # Share the bottom row, giving it whatever width is left rather
            # than a fixed budget that truncates it.
            end_text = _format_end(deal.price.discount_end, now, compact=True)
            if end_text:
                end_right = CARD_WIDTH - PADDING - 10
                if end_right - bottom_x > 80:
                    draw.text(
                        (end_right, top + 84),
                        _truncate(draw, end_text, info_font, end_right - bottom_x),
                        font=info_font,
                        fill=WARN,
                        anchor="ra",
                    )

        # Price block is right aligned so long names never collide with it.
        draw.text(
            (CARD_WIDTH - PADDING - 10, top + 14),
            price_text,
            font=price_font,
            fill=ACCENT,
            anchor="ra",
        )

        if badge_text:
            badge_x = CARD_WIDTH - PADDING - 10 - badge_w
            draw.rounded_rectangle(
                (badge_x, top + 54, badge_x + badge_w, top + 84),
                radius=8,
                fill=DEAL_BG,
            )
            draw.text(
                (badge_x + badge_w / 2, top + 69),
                badge_text,
                font=badge_font,
                fill=(255, 255, 255),
                anchor="mm",
            )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _people(value: int) -> str:
    """Format a player count the way Chinese readers expect.

    Args:
        value: Raw player count.

    Returns:
        A compact string using 万 for ten thousands.
    """
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f} 万"
    return f"{value:,}"


def render_players_card(
    entries: list[PlayerCount],
    capsules: dict[int, bytes] | None = None,
    title: str = "Steam 在线人数排行",
    now: datetime | None = None,
) -> bytes:
    """Render a live player count ranking as a single list image.

    Args:
        entries: Games already sorted by live player count, most first.
        capsules: Mapping of appid to raw capsule image bytes.
        title: Heading shown at the top of the card.
        now: Reference time, defaulting to the current time. Any timezone is
            accepted; the stamp is converted to Beijing time for display.

    Returns:
        PNG image bytes.
    """
    capsules = capsules or {}
    # Stamp in Beijing time: the audience is domestic, and a UTC clock reads as
    # "wrong" rather than as "differently zoned".
    stamp = (now or datetime.now(timezone.utc)).astimezone(_display_timezone())
    count = len(entries)
    # A single game is an info card, not a one-row ranking: the list layout
    # would otherwise read as "rank 1 of 1".
    single = count == 1

    header_height = 96
    row_height = 142 if single else 116
    thumb_size = (196, 110) if single else (150, 84)
    height = header_height + count * row_height + PADDING

    image = Image.new("RGB", (CARD_WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.text((PADDING, 30), title, font=_font(38), fill=TEXT)
    stamp_text = f"{stamp:%H:%M} 北京时间"
    subtitle = f"实时在线 · {stamp_text}" if single else f"共 {count} 款 · 实时数据 {stamp_text}"
    draw.text(
        (CARD_WIDTH - PADDING, 44),
        subtitle,
        font=_font(20),
        fill=TEXT_FAINT,
        anchor="ra",
    )

    rank_font = _font(30)
    name_font = _font(26)
    count_font = _font(32)
    meta_font = _font(19)
    # Reserve the widest realistic count so names get a stable budget.
    count_width = _text_width(draw, "888.8 万", count_font) + 24
    rank_x = PADDING + 14
    # Without a rank column the thumbnail takes its place.
    thumb_x = PADDING + 4 if single else PADDING + 52
    name_x = thumb_x + thumb_size[0] + 18

    for index, entry in enumerate(entries, start=1):
        top = header_height + (index - 1) * row_height
        draw.rounded_rectangle(
            (PADDING - 10, top, CARD_WIDTH - PADDING + 10, top + row_height - 10),
            radius=14,
            fill=PANEL if index % 2 == 1 else PANEL_ALT,
        )

        thumb = _load_capsule(capsules.get(entry.appid), thumb_size, radius=8)
        image.paste(thumb, (thumb_x, top + (row_height - 10 - thumb_size[1]) // 2), thumb)

        # The rank is only meaningful in a list.
        if not single:
            # Top three get the accent colour so the ranking reads at a glance.
            rank_colour = ACCENT if index <= 3 else TEXT_FAINT
            draw.text(
                (rank_x, top + 36),
                str(entry.rank or index),
                font=rank_font,
                fill=rank_colour,
            )

        name_width = CARD_WIDTH - PADDING - 10 - name_x - count_width
        name_y = top + 26 if single else top + 16
        peak_y = top + 68 if single else top + 54
        draw.text(
            (name_x, name_y),
            _truncate(draw, entry.name, name_font, name_width),
            font=name_font,
            fill=TEXT,
        )

        # The chart figure is a separate quantity from the live count (it can
        # even read lower), so it is labelled as Steam's peak rather than
        # anything that would imply a second live reading.
        if entry.peak_today:
            draw.text(
                (name_x, peak_y),
                f"Steam 峰值 {_people(entry.peak_today)}",
                font=meta_font,
                fill=TEXT_FAINT,
            )

        players_text = _people(entry.players) if entry.players else "未公开"
        draw.text(
            (CARD_WIDTH - PADDING - 10, top + (44 if single else 34)),
            players_text,
            font=count_font,
            fill=DISCOUNT if entry.players else TEXT_FAINT,
            anchor="ra",
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_candidates(
    query: str,
    candidates: list,
    command: str,
) -> bytes:
    """Render the disambiguation list shown when a name matches several games.

    Args:
        query: The original user query.
        candidates: Ranked candidates to display.
        command: Command name used in the selection hint.

    Returns:
        PNG image bytes.
    """
    row_height = 74
    header_height = 104
    height = header_height + len(candidates) * row_height + PADDING

    image = Image.new("RGB", (CARD_WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    draw.text((PADDING, 28), f"找到 {len(candidates)} 个匹配", font=_font(34), fill=TEXT)
    draw.text(
        (PADDING, 72),
        _truncate(
            draw,
            f"“{query}”不是唯一的游戏名，请回复序号选择：",
            _font(20),
            CARD_WIDTH - PADDING * 2,
        ),
        font=_font(20),
        fill=TEXT_DIM,
    )

    name_font = _font(26)
    hint_font = _font(20)
    for index, candidate in enumerate(candidates, start=1):
        top = header_height + (index - 1) * row_height
        draw.rounded_rectangle(
            (PADDING - 10, top, CARD_WIDTH - PADDING + 10, top + row_height - 12),
            radius=12,
            fill=PANEL if index % 2 else PANEL_ALT,
        )
        draw.text(
            (PADDING + 6, top + 18),
            f"{index}.",
            font=name_font,
            fill=ACCENT,
        )
        # Show Steam's spelling alongside the localized title when they differ,
        # so the user can tell near-identical entries apart.
        steam_name = getattr(candidate, "steam_name", "") or ""
        label = candidate.display_name if hasattr(candidate, "display_name") else candidate.name
        if steam_name and steam_name.casefold() != label.casefold():
            label = f"{label}（{steam_name}）"
        draw.text(
            (PADDING + 56, top + 18),
            _truncate(draw, label, name_font, 560),
            font=name_font,
            fill=TEXT,
        )
        draw.text(
            (CARD_WIDTH - PADDING - 10, top + 22),
            f"appid {candidate.appid}",
            font=hint_font,
            fill=TEXT_FAINT,
            anchor="ra",
        )

    draw.text(
        (PADDING, height - PADDING - 12),
        f"回复：{command} <序号>  例如 {command} 1",
        font=_font(20),
        fill=DISCOUNT,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

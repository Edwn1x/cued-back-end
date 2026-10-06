"""
Stat cards as a picture: the "image" send mode (stat_cards.py, STAT_CARDS_MODE).

The live bubble's frame is fixed by the Spectrum extension (~268×292pt, too tall for
a three-line card). A static app card instead shows its layout image, and iMessage
sizes that bubble to the image's aspect ratio, so a 2.5:1 picture gives a bubble
about half the height. The SDK builds the static layout from the page's Open Graph
tags (og:image → JPEG), so this module only has to draw the same state the HTML page
renders, from the same stat_cards.build_state().

Trade-offs (founder test 2026-10-05): one theme (dark; an image can't follow the
phone's appearance) and not live (an in-place update swaps in a fresh image).
Drawn at 2× and downsampled, because Pillow's shapes aren't antialiased.
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

W_PT, H_PT = 268, 107          # the bubble's width in points; 2.5:1
OUT_W = 1200                   # SDK caps layout images at 1200px wide
SS = 2                         # supersample factor
S = OUT_W / W_PT * SS          # px per point at draw size

BG = (22, 22, 24)
FG = (245, 245, 247)
MUTED = (152, 152, 159)
TRACK = (58, 58, 60)
CELL = (44, 44, 46)
TONE = {"blue": (47, 107, 246), "amber": (242, 169, 59), "red": (229, 72, 77), "green": (48, 164, 108)}
CHIP = {  # (background, text)
    "exam": ((74, 30, 30), (255, 139, 133)),
    "due": ((74, 53, 18), (245, 194, 107)),
    "lift": ((47, 107, 246), (255, 255, 255)),
    "done": ((23, 56, 38), (111, 211, 155)),
    "more": (CELL, MUTED),
}
BADGE = {"amber": CHIP["due"], "red": CHIP["exam"], "green": CHIP["done"]}

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


@lru_cache(maxsize=64)
def _font(weight: str, pt: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(_FONT_DIR, f"Inter-{weight}.ttf"), int(round(pt * S)))


def _p(v: float) -> int:
    return int(round(v * S))


def _text(d: ImageDraw.ImageDraw, xy, s: str, weight: str, pt: float, fill, anchor="ls"):
    d.text((_p(xy[0]), _p(xy[1])), s, font=_font(weight, pt), fill=fill, anchor=anchor)


def _width(s: str, weight: str, pt: float) -> float:
    return _font(weight, pt).getlength(s) / S


def _fit(s: str, weight: str, pt: float, max_w: float) -> str:
    """Truncate with an ellipsis so `s` fits `max_w` points."""
    if _width(s, weight, pt) <= max_w:
        return s
    while s and _width(s + "…", weight, pt) > max_w:
        s = s[:-1]
    return (s + "…") if s else ""


def _bar(d, x, y, w, h, frac, tone):
    d.rounded_rectangle((_p(x), _p(y), _p(x + w), _p(y + h)), radius=_p(h / 2), fill=TRACK)
    if frac:
        fw = max(h, w * min(max(frac, 0), 1))
        d.rounded_rectangle((_p(x), _p(y), _p(x + fw), _p(y + h)), radius=_p(h / 2), fill=TONE.get(tone, TONE["blue"]))


def _pill(d, x_right, y_mid, s, colors, pt=10):
    """A badge right-aligned at x_right; returns its left edge."""
    w = _width(s, "SemiBold", pt) + 12
    h = pt + 6
    x = x_right - w
    d.rounded_rectangle((_p(x), _p(y_mid - h / 2), _p(x_right), _p(y_mid + h / 2)), radius=_p(h / 2), fill=colors[0])
    _text(d, (x + w / 2, y_mid), s, "SemiBold", pt, colors[1], anchor="mm")
    return x


PAD = 14
# The Spectrum launcher icon sits over the picture's top-left (~32pt, phone 2026-10-05:
# it covered "rsf r", "tod"), so the label row starts after it.
LABEL_X = PAD + 32


def _draw_rsf(d, s):
    _text(d, (LABEL_X, 24), s["label"], "Regular", 13, MUTED)
    if s.get("foot"):
        _text(d, (W_PT - PAD, 24), s["foot"], "Regular", 11, MUTED, anchor="rs")
    head = s.get("headline") or ""
    _text(d, (PAD, 64), head, "Bold", 30, FG)
    if s.get("subline"):
        x = PAD + _width(head, "Bold", 30) + 8
        room = W_PT - PAD - x
        pt = 13 if _width(s["subline"], "Regular", 13) <= room else 11   # "line's on · ~25 min wait." is long
        _text(d, (x, 64), _fit(s["subline"], "Regular", pt, room), "Regular", pt, MUTED)
    for b in s.get("bars") or []:
        _bar(d, PAD, H_PT - PAD - 8, W_PT - 2 * PAD, 8, b.get("frac"), b.get("tone"))


def _draw_macros(d, s):
    _text(d, (LABEL_X, 24), s["label"], "Regular", 13, MUTED)
    bars = s.get("bars") or []
    gap = 16
    col_w = (W_PT - 2 * PAD - gap) / 2
    for i, b in enumerate(bars[:2]):
        x = PAD + i * (col_w + gap)
        _text(d, (x, 46), b["label"], "Regular", 12, MUTED)
        if b.get("badge"):
            _pill(d, x + col_w, 42, b["badge"], BADGE.get(b.get("badge_tone"), CHIP["more"]))
        big = b.get("big") or b.get("value") or ""
        of = (b.get("of") or "").replace(" cal", "")          # the column is narrow; the label says calories
        _text(d, (x, 74), big, "Bold", 24, FG)
        if of:
            bx = x + _width(big, "Bold", 24) + 4
            _text(d, (bx, 74), _fit(of, "Regular", 11, x + col_w - bx), "Regular", 11, MUTED)
        if b.get("frac") is not None:
            _bar(d, x, H_PT - PAD - 7, col_w, 7, b["frac"], b.get("tone"))
    if s.get("no_targets"):
        _text(d, (W_PT - PAD, 24), "no targets set yet", "Regular", 11, MUTED, anchor="rs")


def _draw_week(d, s):
    _text(d, (LABEL_X, 24), s["label"], "Regular", 13, MUTED)
    days = s.get("week") or []
    n = max(len(days), 1)
    gap = 4
    col_w = (W_PT - 2 * PAD - gap * (n - 1)) / n
    top, bottom = 44, H_PT - 10
    for i, day in enumerate(days):
        x = PAD + i * (col_w + gap)
        _text(d, (x + col_w / 2, 38), day["dow"], "SemiBold" if day.get("today") else "Regular", 11,
              TONE["blue"] if day.get("today") else MUTED, anchor="ms")
        d.rounded_rectangle((_p(x), _p(top), _p(x + col_w), _p(bottom)), radius=_p(7), fill=CELL)
        chips = list(day.get("items") or [])
        if day.get("more"):
            chips.append({"text": f"+{day['more']}", "tone": "more"})
        heads = [c for c in chips if c["tone"] not in ("lift", "done")]
        feet = [c for c in chips if c["tone"] in ("lift", "done")]   # lifts sit at the bottom, like the page
        ch = 15
        for j, c in enumerate(heads):
            _chip(d, x + 2, top + 3 + j * (ch + 3), col_w - 4, ch, c)
        for j, c in enumerate(reversed(feet)):
            _chip(d, x + 2, bottom - 3 - ch - j * (ch + 3), col_w - 4, ch, c)
    if s.get("empty_note"):
        note = s["empty_note"]
        if _width(note, "Regular", 11) > W_PT - 2 * PAD - 16:
            note = "nothing due · tell me your lift days" if "lift days" in note else _fit(note, "Regular", 11, W_PT - 2 * PAD - 16)
        w = _width(note, "Regular", 11) + 16
        mid = (top + bottom) / 2
        d.rounded_rectangle((_p(W_PT / 2 - w / 2), _p(mid - 11), _p(W_PT / 2 + w / 2), _p(mid + 11)), radius=_p(11), fill=BG)
        _text(d, (W_PT / 2, mid), note, "Regular", 11, MUTED, anchor="mm")


def _chip(d, x, y, w, h, c):
    bg, fg = CHIP.get(c["tone"], CHIP["more"])
    d.rounded_rectangle((_p(x), _p(y), _p(x + w), _p(y + h)), radius=_p(4), fill=bg)
    _text(d, (x + w / 2, y + h / 2), _fit(c["text"], "SemiBold", 10, w - 3), "SemiBold", 10, fg, anchor="mm")


DRAW = {"rsf": _draw_rsf, "macros": _draw_macros, "week": _draw_week}


def _draw(state: dict) -> Image.Image:
    img = Image.new("RGB", (_p(W_PT), _p(H_PT)), BG)
    DRAW[state["kind"]](ImageDraw.Draw(img), state)
    return img.resize((OUT_W, int(round(OUT_W * H_PT / W_PT))), Image.LANCZOS)


def render_png(state: dict) -> bytes:
    """The card as a 1200×479 PNG (dark): og:image / link previews."""
    buf = io.BytesIO()
    _draw(state).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_jpeg(state: dict) -> bytes:
    """The card as JPEG: the bubble's layout image (the SDK's own previews are JPEG)."""
    buf = io.BytesIO()
    _draw(state).save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()

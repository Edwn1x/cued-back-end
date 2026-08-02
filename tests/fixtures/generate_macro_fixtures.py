"""
Generate the macro-accuracy Phase A tier-2 fixtures (committed PNGs; rerun to rebuild).

    python3 tests/fixtures/generate_macro_fixtures.py

- nutrition_label.png — a granola nutrition-facts panel. Printed numbers are the
  ground truth the visible-label case asserts against (serving 55g / 240 cal /
  6g protein / 8 servings).
- plate_meal.png — overhead plate (clearly a standard dinner plate) with a fork
  beside it and three food regions. Synthetic on purpose (same trade as
  tenders_label.png): deterministic and legible, but stylized — realism is a
  first-funded-run validation item, noted in rewrite/macro-accuracy/SUMMARY-A.md.
"""

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(__file__)


def _font(size):
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _font_bold(size):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def nutrition_label(path):
    img = Image.new("RGB", (520, 720), "#f4f0e6")
    d = ImageDraw.Draw(img)
    d.rectangle([30, 20, 490, 700], outline="black", width=4)
    d.text((50, 40), "HONEY OAT GRANOLA", font=_font_bold(34), fill="black")
    d.text((50, 92), "Nutrition Facts", font=_font_bold(44), fill="black")
    d.line([50, 150, 470, 150], fill="black", width=8)
    rows = [
        ("Serving size", "1/2 cup (55g)"),
        ("Servings per container", "8"),
    ]
    y = 170
    for k, v in rows:
        d.text((50, y), k, font=_font(26), fill="black")
        d.text((470, y), v, font=_font(26), fill="black", anchor="ra")
        y += 44
    d.line([50, y, 470, y], fill="black", width=4)
    d.text((50, y + 16), "Calories", font=_font_bold(40), fill="black")
    d.text((470, y + 16), "240", font=_font_bold(48), fill="black", anchor="ra")
    y += 90
    d.line([50, y, 470, y], fill="black", width=8)
    rows = [
        ("Total Fat", "9g"),
        ("Saturated Fat", "1g"),
        ("Sodium", "15mg"),
        ("Total Carbohydrate", "37g"),
        ("Dietary Fiber", "4g"),
        ("Total Sugars", "12g"),
        ("Protein", "6g"),
    ]
    for k, v in rows:
        y += 46
        d.text((50, y), k, font=_font(28), fill="black")
        d.text((470, y), v, font=_font_bold(28), fill="black", anchor="ra")
        d.line([50, y + 38, 470, y + 38], fill="black", width=1)
    img.save(path)


def plate_meal(path):
    img = Image.new("RGB", (900, 700), "#8a6a4f")  # wood table
    d = ImageDraw.Draw(img)
    # standard ~10.5in dinner plate, overhead
    d.ellipse([120, 60, 720, 660], fill="#f5f5f2", outline="#d9d9d4", width=6)
    d.ellipse([170, 110, 670, 610], outline="#e4e4de", width=4)  # inner rim
    # rice: off-white mound, left half
    d.ellipse([210, 200, 460, 470], fill="#efe9d8", outline="#e0d8c2", width=3)
    for gx, gy in [(260, 260), (310, 240), (360, 300), (300, 350), (390, 380),
                   (250, 320), (350, 430), (410, 250), (280, 410)]:
        d.ellipse([gx, gy, gx + 14, gy + 8], fill="#faf6ea")
    # grilled chicken: two brown strips, right side
    for box in ([470, 210, 640, 300], [480, 320, 660, 410]):
        d.rounded_rectangle(box, radius=28, fill="#a9713d", outline="#8a5527", width=4)
        for i in range(3):
            x0 = box[0] + 20 + i * 45
            d.line([x0, box[1] + 12, x0 + 24, box[3] - 12], fill="#8a5527", width=5)
    # broccoli: green florets, bottom
    for cx, cy, r in [(400, 520, 42), (470, 545, 38), (540, 510, 44), (340, 545, 34)]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#4c7a3d", outline="#3c6130", width=3)
        d.rectangle([cx - 8, cy + r - 6, cx + 8, cy + r + 18], fill="#6d9c5a")
    # fork beside the plate (a second reference object)
    d.rounded_rectangle([780, 180, 806, 560], radius=12, fill="#c9c9cf", outline="#a8a8b0", width=2)
    for i in range(4):
        x0 = 776 + i * 9
        d.rounded_rectangle([x0, 120, x0 + 6, 200], radius=3, fill="#c9c9cf")
    img.save(path)


if __name__ == "__main__":
    nutrition_label(os.path.join(HERE, "nutrition_label.png"))
    plate_meal(os.path.join(HERE, "plate_meal.png"))
    print("wrote nutrition_label.png, plate_meal.png")

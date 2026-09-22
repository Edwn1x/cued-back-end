"""Renders a MyNetDiary-style breakfast diary screenshot (tests/fixtures/diary_breakfast.png)
for the logger-bridge live anchors. Printed numbers are the ground truth the coach must
copy, not estimate. Regenerate: python3 tests/fixtures/generate_diary_fixture.py"""
from PIL import Image, ImageDraw, ImageFont
import os

W, H = 720, 900
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)
try:
    big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 34)
    med = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
    small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
except Exception:
    big = med = small = ImageFont.load_default()

d.rectangle([0, 0, W, 90], fill=(28, 120, 210))
d.text((24, 26), "MyNetDiary", fill="white", font=big)
d.text((W - 140, 32), "Today", fill="white", font=small)
d.text((24, 120), "Breakfast", fill=(20, 20, 20), font=big)
d.text((W - 220, 128), "551 Cals", fill=(20, 20, 20), font=med)
rows = [
    ("Sprouted Multigrain Bread, 1 slice", "80"),
    ("Liquid Egg Whites, 130 g", "68"),
    ("Herb Roasted Turkey Breast, 2 servings", "100"),
    ("American Cheese, 1 slice", "60"),
    ("Mini Avocado, 1 each", "80"),
    ("Reduced-Fat Mayonnaise, 1 tbsp", "35"),
    ("Romaine Lettuce, 1 cup", "8"),
    ("Blueberries, 1 cup", "120"),
]
y = 190
for name, cal in rows:
    d.text((24, y), name, fill=(40, 40, 40), font=med)
    d.text((W - 120, y), cal, fill=(40, 40, 40), font=med)
    d.line([24, y + 42, W - 24, y + 42], fill=(225, 225, 225))
    y += 60
y += 30
d.text((24, y), "Breakfast totals", fill=(20, 20, 20), font=med)
y += 44
d.text((24, y), "Calories 551    Protein 53.5 g    Carbs 44.4 g    Fat 16.7 g", fill=(60, 60, 60), font=small)
out = os.path.join(os.path.dirname(__file__), "diary_breakfast.png")
img.save(out)
print("wrote", out)

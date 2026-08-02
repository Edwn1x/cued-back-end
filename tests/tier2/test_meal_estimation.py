"""
Tier-2 (live) — macro-accuracy Phase A: reference-object scaling + label reading.

Tier-1 pinned the prompt-block composition; these check the MODEL uses the technique:
a photo estimate reasons from in-frame reference objects (portion visible in the log,
not just a bare food name), a legible nutrition label beats eyeballing, and the
existing logging path is unbroken with the flag off. Accuracy itself isn't
unit-testable — USING the reference/label is what's asserted.

Run: pytest --run-tier2 -s tests/tier2/test_meal_estimation.py
Fixtures (synthetic, committed; regenerate via tests/fixtures/generate_macro_fixtures.py):
- plate_meal.png       — overhead ~10.5in dinner plate: rice, grilled chicken, broccoli; fork.
- nutrition_label.png  — granola panel: 1/2 cup (55g)/serving, 8 servings, 240 cal, 6g protein.
Fixture realism is a known first-run risk: if the model balks at the stylized plate,
swap in a real photo before concluding the prompt failed.
"""

from __future__ import annotations

import base64
import os

import pytest

pytestmark = pytest.mark.tier2

_FIXDIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

# Portion-basis vocabulary: any of these in the logged description (or reply) shows
# the estimate was sized, not just named. The prompt asks for portion in the log.
_PORTION_MARKERS = ("cup", "oz", "ounce", "gram", " g ", "palm", "fist", "plate",
                    "tbsp", "serving")


def _img(name) -> dict:
    with open(os.path.join(_FIXDIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _enable(monkeypatch, **flags):
    import config
    defaults = dict(READ_IMAGE_ENABLED=True, MEAL_ESTIMATION_PROMPT_ENABLED=True,
                    LOG_MEAL_TOOL_ENABLED=True, REMEMBER_TOOL_ENABLED=True,
                    MANAGE_LOG_TOOL_ENABLED=True)
    defaults.update(flags)
    for name, val in defaults.items():
        monkeypatch.setattr(config, name, val)


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).all()
    finally:
        s.close()


def test_reference_object_photo_yields_portioned_estimate(db, monkeypatch):
    """A plate photo with clear reference objects (standard plate, fork) must produce
    a PORTIONED log — 'chicken ~6oz, rice ~1.5 cups', not bare 'chicken and rice'.
    The number being right isn't assertable; the portion basis being present is."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "lunch", "freeform", image_data=_img("plate_meal.png"))
    print(f"\n[PLATE reply] {reply!r}")

    meals = _meals(user.id)
    for m in meals:
        print(f"[PLATE logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
    assert meals, f"plate photo logged no meal: {reply!r}"

    haystack = (" ".join(m.description or "" for m in meals) + " " + reply).lower()
    assert any(mk in haystack for mk in _PORTION_MARKERS), \
        f"estimate never states a portion basis (log + reply): {haystack!r}"


def test_visible_label_beats_eyeballing(db, monkeypatch):
    """A legible nutrition label is ground truth: 'two servings' of the granola
    (2 × 240 cal / 6g protein) must be logged from the printed numbers, scaled —
    not re-estimated visually."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "just had two servings of this granola", "freeform",
                           image_data=_img("nutrition_label.png"))
    print(f"\n[LABEL reply] {reply!r}")

    meals = _meals(user.id)
    for m in meals:
        print(f"[LABEL logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
    assert meals, f"label photo logged no meal: {reply!r}"

    total_cal = sum(m.calories or 0 for m in meals)
    total_pro = sum(m.protein_g or 0 for m in meals)
    # 2 × 240 = 480 cal, 2 × 6 = 12g. Tolerance for rounding, zero tolerance for a
    # generic granola guess (typically ~600+ for "two servings" eyeballed, or ~240
    # if the serving math was dropped).
    assert 430 <= total_cal <= 530, \
        f"label math not used: logged {total_cal} cal (expected ~480 from 2 × 240): {reply!r}"
    assert 10 <= total_pro <= 15, \
        f"label protein not used: logged {total_pro}g (expected ~12): {reply!r}"


def test_plain_photo_still_logs_with_flag_off(db, monkeypatch):
    """No-regression guard: with MEAL_ESTIMATION_PROMPT_ENABLED off, the existing
    image → log_meal path behaves as before (a meal still logs)."""
    _enable(monkeypatch, MEAL_ESTIMATION_PROMPT_ENABLED=False)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "dinner just now", "freeform", image_data=_img("plate_meal.png"))
    print(f"\n[FLAG-OFF reply] {reply!r}")

    assert _meals(user.id), f"existing path regressed — no meal logged with flag off: {reply!r}"

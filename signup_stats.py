"""
Signup-form stats → User columns (founder, 2026-09-30 / 10-01).

The objective, pick-list facts belong on the form, not in the first conversation:
height and weight (a user who texts "168 cm" to an extractor that only knows feet
sits in onboarding forever — user 47), days a week, preferred training time, diet,
allergies / foods they won't eat, apps or wearables, average daily steps. The coach
still gets to know the person over text; it just never has to ask for a number the
form already holds, and `_get_missing_fields` skips every column filled here.

All fields are OPTIONAL. Anything present is validated and rejected on nonsense (never
truncated or coerced — a silently clipped value is a wrong fact for months). The site
can add fields one at a time; nothing here is required for /waitlist to accept a row.

Accepted keys (any subset):
  height_cm | height_ft (+ height_in)       → height_ft, height_in
  weight_kg | weight_lbs                    → weight_lbs
  workout_days   1–7, or a list/csv of day names ("Mon,Wed,Fri")   → workout_days
  workout_time   morning | afternoon | evening | HH:MM              → workout_time
  injuries       free text; present-but-empty means "none"          → injuries
  diet           omnivore | vegetarian | vegan | pescatarian | keto | halal | kosher | none(→omnivore)
  restrictions   free text (allergies, won't-eat); list or csv       → restrictions
  existing_tools list/csv of app / device names; "none"             → existing_tools, tools_decision
  avg_steps      0–50000                                             → avg_steps
"""

from __future__ import annotations

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DIETS = {"omnivore", "vegetarian", "vegan", "pescatarian", "keto", "halal", "kosher"}
_TIME_WORDS = {"morning": "08:00", "afternoon": "14:00", "evening": "18:00", "night": "20:00"}
_TEXT_CAP = 500


def cm_to_ft_in(cm) -> tuple[int | None, int | None]:
    """168 → (5, 6). None outside 4'0–8'0."""
    try:
        total_in = round(float(str(cm).replace(",", ".")) / 2.54)
    except (TypeError, ValueError):
        return None, None
    if not 48 <= total_in <= 96:
        return None, None
    ft, inch = divmod(total_in, 12)
    return ft, inch


def kg_to_lbs(kg) -> float | None:
    """50 → 110.0 (rounded to the pound). None outside 60–600 lb."""
    try:
        lbs = round(float(str(kg).replace(",", ".")) * 2.20462)
    except (TypeError, ValueError):
        return None
    return float(lbs) if 60 <= lbs <= 600 else None


def _present(data: dict, key: str) -> bool:
    v = data.get(key)
    return v is not None and v != "" and v != []


def _num(data: dict, key: str) -> float | None:
    if not _present(data, key):
        return None
    try:
        return float(str(data[key]).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(key)


def _csv(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = str(value).split(",")
    return [p.strip() for p in parts if p and p.strip()]


def stats_from_form(data: dict) -> tuple[dict, str | None]:
    """→ (columns to set on User, None) or ({}, error message for the form)."""
    cols: dict = {}
    try:
        cm, ft, inch = _num(data, "height_cm"), _num(data, "height_ft"), _num(data, "height_in")
        kg, lbs = _num(data, "weight_kg"), _num(data, "weight_lbs")
        steps = _num(data, "avg_steps")
    except ValueError as e:
        return {}, f"That {str(e).replace('_', ' ')} doesn't look right."

    if cm is not None:
        f, i = cm_to_ft_in(cm)
        if f is None:
            return {}, "That height doesn't look right."
        cols["height_ft"], cols["height_in"] = f, i
    elif ft is not None:
        if not 4 <= ft <= 7 or (inch is not None and not 0 <= inch <= 11):
            return {}, "That height doesn't look right."
        cols["height_ft"], cols["height_in"] = int(ft), int(inch or 0)

    if kg is not None:
        w = kg_to_lbs(kg)
        if w is None:
            return {}, "That weight doesn't look right."
        cols["weight_lbs"] = w
    elif lbs is not None:
        if not 60 <= lbs <= 600:
            return {}, "That weight doesn't look right."
        cols["weight_lbs"] = float(round(lbs, 1))

    if _present(data, "workout_days"):
        wd = data["workout_days"]
        if isinstance(wd, (int, float)) or (isinstance(wd, str) and wd.strip().isdigit()):
            n = int(float(wd))
            if not 1 <= n <= 7:
                return {}, "Those training days don't look right."
            cols["workout_days"] = str(n)
        else:
            keys = [p.lower()[:3] for p in _csv(wd)]
            if not keys or any(k not in _DAYS for k in keys):
                return {}, "Those training days don't look right."
            cols["workout_days"] = ",".join(dict.fromkeys(keys))

    if _present(data, "workout_time"):
        t = str(data["workout_time"]).strip().lower()
        if t in _TIME_WORDS:
            cols["workout_time"] = _TIME_WORDS[t]
        else:
            import re
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
            if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
                return {}, "That training time doesn't look right."
            cols["workout_time"] = f"{int(m.group(1)):02d}:{m.group(2)}"

    if "injuries" in data and data["injuries"] is not None:
        inj = str(data["injuries"]).strip()
        if len(inj) > _TEXT_CAP:
            return {}, "That injuries note is too long."
        cols["injuries"] = inj or "none"

    if _present(data, "diet"):
        d = str(data["diet"]).strip().lower()
        if d in ("none", "no restrictions", "no_restrictions"):
            d = "omnivore"
        if d not in _DIETS:
            return {}, "That diet doesn't look right."
        cols["diet"] = d

    if _present(data, "restrictions"):
        r = "; ".join(_csv(data["restrictions"]))
        if len(r) > _TEXT_CAP:
            return {}, "That restrictions note is too long."
        if r:
            cols["restrictions"] = r

    if _present(data, "existing_tools"):
        tools = [t.lower().replace(" ", "_") for t in _csv(data["existing_tools"])]
        if any(len(t) > 40 for t in tools):
            return {}, "Those apps don't look right."
        if not tools or tools == ["none"]:
            cols["existing_tools"], cols["tools_decision"] = "none", "none"
        else:
            cols["existing_tools"] = ",".join(dict.fromkeys(t for t in tools if t != "none"))
            cols["tools_decision"] = "acknowledged"

    if steps is not None:
        if not 0 <= steps <= 50000:
            return {}, "That step count doesn't look right."
        cols["avg_steps"] = int(steps)

    return cols, None

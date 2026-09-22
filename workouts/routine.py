"""
Routine capture — a pasted program becomes the user's own card templates.

Live 2026-09-22 (user 42): a six-day PPL with every exercise, set and rep was pasted
during onboarding and survived only as current_split="ppl". The card would have shown
the generic bench/incline/fly day. users.custom_templates (PR #86) is the home; this
module fills it — from the onboarding path (no tools there: a code trigger on
routine-shaped text) and from the coach loop (save_routine tool).

Parsing is a small Sonnet JSON call (the model reads "Tue/pull (back and bi/tri)" and
"Pull up 4x5 (goal x10)" better than any regex); everything it returns is validated
by code: split keys must be real, sets/reps positive ints, and weights are NEVER taken
from the model — a known movement gets the global template's default, anything else
gets a conservative starting load, and bodyweight/timed work gets 0 with rep-progression.
The user's real numbers replace the placeholders the first time they log a set.
"""

from __future__ import annotations

import json
import logging
import re

import config
from models import User, get_session
from workouts.templates import TEMPLATES, custom_templates_for, normalize_template_key

logger = logging.getLogger("cued.routine")

# Routine-shaped text: several lines carrying a sets×reps token, or a split-day heading.
_SETS_REPS_RE = re.compile(r"\b\d{1,2}\s*[x×]\s*\d{1,3}\b|\b\d{1,2}\s*sets?\b", re.IGNORECASE)
_DAY_HEAD_RE = re.compile(r"^\s*(?:\w{3,9}\s*[/:\-–]\s*)?(push|pull|legs?|upper|lower|full\s*body)\b", re.IGNORECASE | re.MULTILINE)
MIN_ROUTINE_LINES = 4


def looks_like_routine(text: str) -> bool:
    """≥4 lines with a sets×reps token, or ≥2 split-day headings and ≥2 such lines."""
    if not text or "\n" not in text:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sr = sum(1 for ln in lines if _SETS_REPS_RE.search(ln))
    heads = len(_DAY_HEAD_RE.findall(text))
    return sr >= MIN_ROUTINE_LINES or (heads >= 2 and sr >= 2)


_PARSE_PROMPT = """Parse this workout routine the user pasted into structured split days. Return ONLY JSON.

Routine text:
\"\"\"{text}\"\"\"

Rules:
- Split each day's exercises under one of these keys ONLY: push, pull, legs, upper, lower, full_body. Map headings like "Mon/Push (chest and shoulders)" → push, "Tue/pull" → pull, "Wed/Legs" → legs, "chest day" → push, "back day" → pull. Days that fit none of these keys are dropped.
- If the same key appears twice (a high-volume and a low-volume week), keep the FIRST occurrence.
- For each exercise: name (as written, cleaned), sets (int), reps (int; for a range like "8-10" use the low end; for a time like "1 min" or "3 min song" give seconds as reps and set "timed": true), and "bodyweight": true for pull ups, planks, dead hangs, dips, push ups, hanging/ab work with no load, forearm squeezes.
- Ignore warm-up notes, "(goal x10)", "+ (warmup)", runs, swims, and "rest day" lines.
- Keep the order as written.

Return: {{"days": {{"push": [{{"name": "...", "sets": 3, "reps": 10, "bodyweight": false, "timed": false}}, ...], "pull": [...], ...}}}}"""


# Conservative starting loads for movements the global templates don't know. Plate-
# loadable, beginner-safe; the first logged set replaces them. Keys are substrings of
# the lowercased exercise name; first match wins (order matters: longer first).
_STARTING_LOADS: list[tuple[str, float, float]] = [
    ("bulgarian", 25, 5), ("split squat", 25, 5), ("smith", 95, 10), ("hack squat", 90, 10),
    ("goblet", 35, 5), ("front squat", 95, 10), ("sumo", 135, 10), ("hip thrust", 135, 10),
    ("leg extension", 70, 5), ("quad extension", 70, 5), ("extension", 70, 5),
    ("adductor", 70, 5), ("abductor", 70, 5), ("hamstring", 70, 5), ("leg curl", 70, 5),
    ("toe press", 180, 10), ("calf", 90, 10),
    ("shoulder press", 30, 5), ("arnold", 25, 5), ("lateral raise", 10, 5), ("front raise", 10, 5),
    ("rear delt", 15, 5), ("reverse fly", 15, 5), ("y raise", 10, 5), ("upright row", 45, 5),
    ("shrug", 50, 5), ("face pull", 30, 5), ("pullover", 30, 5),
    ("incline", 40, 5), ("decline", 95, 5), ("chest press", 90, 5), ("pec deck", 60, 5),
    ("fly", 20, 5), ("flye", 20, 5), ("crossover", 20, 5), ("dip", 0, 0),
    ("skull", 40, 5), ("overhead", 30, 5), ("pushdown", 40, 5), ("pulldown", 40, 5),
    ("kickback", 10, 5), ("tricep", 30, 5),
    ("hammer", 25, 5), ("preacher", 40, 5), ("concentration", 20, 5), ("curl", 30, 5),
    ("seated row", 80, 5), ("cable row", 80, 5), ("t-bar", 70, 10), ("pendlay", 95, 5),
    ("dumbbell row", 40, 5), ("db row", 40, 5), ("row", 60, 5),
    ("lat", 100, 5), ("chin", 0, 0), ("pull up", 0, 0), ("pullup", 0, 0), ("pull-up", 0, 0),
    ("press", 40, 5), ("squat", 95, 10), ("deadlift", 135, 10), ("rdl", 135, 10), ("lunge", 25, 5),
    ("abs", 10, 5), ("crunch", 10, 5), ("leg raise", 0, 0), ("plank", 0, 0), ("hang", 0, 0),
]


# Only these phrases map onto a GLOBAL template slug (keeping its default load, plate step
# and any history). A qualifier that makes it a different movement blocks the alias:
# "squats smith" is not the barbell squat, "rkc plank" is not the plank, "hammer curls"
# are not barbell curls. Everything else gets its own slug + a starting load.
_GLOBAL_ALIASES: list[tuple[str, str]] = [
    ("incline dumbbell press", "incline_db_press"), ("dumbbell incline press", "incline_db_press"),
    ("incline db press", "incline_db_press"), ("incline press", "incline_db_press"),
    ("bench press", "bench_press"), ("flat bench", "bench_press"),
    ("cable fly", "cable_fly"), ("cable flye", "cable_fly"), ("cable crossover", "cable_fly"),
    ("tricep pushdown", "tricep_pushdown"), ("tricep pulldown", "tricep_pushdown"), ("rope pushdown", "tricep_pushdown"),
    ("romanian deadlift", "romanian_deadlift"), ("rdl", "romanian_deadlift"),
    ("deadlift", "deadlift"), ("barbell row", "barbell_row"), ("bent over row", "barbell_row"),
    ("lat pulldown", "lat_pulldown"), ("pulldown", "lat_pulldown"),
    ("face pull", "face_pull"), ("barbell curl", "barbell_curl"), ("bb curl", "barbell_curl"),
    ("overhead press", "overhead_press"), ("ohp", "overhead_press"), ("military press", "overhead_press"),
    ("leg press", "leg_press"), ("leg curl", "leg_curl"), ("calf raise", "calf_raise"),
    ("back squat", "squat"), ("barbell squat", "squat"), ("squat", "squat"),
    ("push up", "pushup"), ("pushup", "pushup"), ("plank", "plank"),
]
_ALIAS_BLOCKERS = {"smith", "hack", "front", "goblet", "bulgarian", "split", "sumo", "pause", "paused",
                   "pin", "box", "zercher", "landmine", "trap", "rkc", "weighted", "decline", "reverse",
                   "hammer", "preacher", "spider", "overhead", "toe", "single", "one", "pistol", "sissy",
                   "jump", "wall", "side", "copenhagen", "nordic", "glute", "seated", "lying", "kickback"}
# ("seated"/"lying" block leg_curl aliasing on purpose: the machine differs; they keep their own slug.)


def _norm_words(name: str) -> list[str]:
    words = re.findall(r"[a-z]+", (name or "").lower())
    out = []
    for w in words:
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _global_slug_for(name: str) -> str | None:
    words = _norm_words(name)
    if not words or set(words) & _ALIAS_BLOCKERS:
        return None
    joined = " ".join(words)
    for phrase, slug in _GLOBAL_ALIASES:
        pw = " ".join(_norm_words(phrase))
        if re.search(rf"(?<![a-z]){re.escape(pw)}(?![a-z])", joined):
            return slug
    return None


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s[:40] or "exercise"


def _global_default(slug: str):
    for exs in TEMPLATES.values():
        for e in exs:
            if e.slug == slug:
                return e.default_weight, e.plate_step
    return None


def _starting_load(name: str) -> tuple[float, float]:
    n = (name or "").lower()
    for key, w, step in _STARTING_LOADS:
        if key in n:
            return float(w), float(step)
    return 30.0, 5.0


def _row(ex: dict) -> dict | None:
    name = str(ex.get("name") or "").strip()
    try:
        sets, reps = int(ex.get("sets") or 0), int(ex.get("reps") or 0)
    except (TypeError, ValueError):
        return None
    if not name or sets <= 0 or reps <= 0:
        return None
    bodyweight, timed = bool(ex.get("bodyweight")), bool(ex.get("timed"))
    label = name.lower()
    if timed and "sec" not in label:
        label = f"{label} (sec)"
    # A slug the global templates know keeps its history/plate-step contract — but
    # only through the explicit alias table (see _GLOBAL_ALIASES / _ALIAS_BLOCKERS).
    slug = _global_slug_for(name) or _slugify(name)
    if bodyweight or timed:
        return {"slug": slug, "label": label, "sets": sets, "reps": reps,
                "default_weight": 0, "plate_step": 0, "rep_step": 10 if timed else 1}
    known = _global_default(slug)
    weight, step = known if known else _starting_load(name)
    return {"slug": slug, "label": label, "sets": sets, "reps": reps,
            "default_weight": weight, "plate_step": step}


def _split_for(keys: set) -> str | None:
    if {"push", "pull", "legs"} <= keys:
        return "ppl"
    if {"upper", "lower"} <= keys:
        return "upper_lower"
    if "full_body" in keys:
        return "full_body"
    return "custom" if keys else None


def parse_routine(text: str, user_id: int | None = None) -> dict:
    """Sonnet JSON → validated custom_templates dict ({key: [rows]}); {} on failure."""
    try:
        from agent_loop import _join_text
        from cost_tracking import track
        from llm_client import make_client
        client = make_client()
        resp = client.messages.create(model=config.ONBOARDING_EXTRACTOR_MODEL, max_tokens=3000,
                                      messages=[{"role": "user", "content": _PARSE_PROMPT.format(text=text[:6000])}])
        try:
            track(user_id, "routine.parse", config.ONBOARDING_EXTRACTOR_MODEL, resp)
        except Exception as e:  # noqa: BLE001
            logger.warning("ROUTINE_COST_TRACK_FAILED user=%s err=%s", user_id, e)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            logger.warning("ROUTINE_PARSE_TRUNCATED user=%s", user_id)
            return {}
        raw = (_join_text(resp.content) or "").replace("```json", "").replace("```", "").strip()
        data = json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("ROUTINE_PARSE_FAILED user=%s err=%s", user_id, e)
        return {}
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, dict):
        return {}
    out: dict[str, list] = {}
    for key, exs in days.items():
        k = normalize_template_key(key)
        if not k or not isinstance(exs, list) or k in out:
            continue
        rows = [r for r in (_row(e) for e in exs if isinstance(e, dict)) if r]
        if rows:
            out[k] = rows
    return out


def save_routine(user_id: int, text: str, *, source: str) -> dict:
    """Parse + merge into users.custom_templates (day-level replace; days not in the
    paste are kept). Sets current_split when it's unknown. Returns
    {"days": {key: n_exercises}, "split": ...} or {"error": ...}."""
    parsed = parse_routine(text, user_id)
    if not parsed:
        return {"error": "couldn't read a routine out of that"}
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        merged = dict(user.custom_templates or {})
        merged.update(parsed)
        # Validate through the same reader the card uses — anything it drops, we drop.
        class _U:
            custom_templates = merged
        valid = custom_templates_for(_U())
        if not valid:
            return {"error": "no valid days"}
        user.custom_templates = {k: merged[k] for k in valid}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(user, "custom_templates")
        split = _split_for(set(user.custom_templates))
        if split and (user.current_split in (None, "", "none", "custom")):
            user.current_split = split
        session.commit()
        summary = {k: len(v) for k, v in valid.items()}
        logger.info("ROUTINE_SAVED user=%s source=%s days=%s split=%s", user_id, source, summary, user.current_split)
        return {"days": summary, "split": user.current_split}
    finally:
        session.close()


def describe_routine(custom_templates: dict | None) -> str | None:
    """One line per day for prompts/summaries: 'push: 10 exercises (dumbbell incline press, …)'."""
    class _U:
        pass
    _U.custom_templates = custom_templates or {}
    valid = custom_templates_for(_U())
    if not valid:
        return None
    lines = []
    for k in ("push", "pull", "legs", "upper", "lower", "full_body"):
        if k in valid:
            names = ", ".join(e.label for e in valid[k][:3])
            lines.append(f"{k}: {len(valid[k])} exercises ({names}{', …' if len(valid[k]) > 3 else ''})")
    return "\n".join(lines)

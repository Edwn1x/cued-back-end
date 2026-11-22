"""
Model in the loop at interpretive moments; code as the bounds (interpret.py).

Two moments in the first days of every thread were decided by a regex or a formula where
reading was needed, and the 11-thread audit (2026-11-21) shows the cost:

  - the first-card answer. "like no clue i think heaviest squat was 135" was parsed as a 5-rep
    working weight → squat 4×5 at 135 for a never-trained 115-lb woman (u56). "All time PRs:
    Bench 185… This morning I did 3x10 of 135" started the card at 165 (u77). A regex cannot
    read a hedge, a max, or "this morning".
  - the first targets. The calculator handed 4150 cal to a 5'4" 200-lb 17-year-old cutting
    (u40), 3400 to a 320-lb sedentary man (u80), 3550 to a 220-lb one-meal-a-day volleyball
    player (u86), 3100 to a 165-lb student who eats ~2000 on a budget (u77). Every one was
    corrected by a human within the hour, to roughly 60–75% of the formula.

Here the model reads first and code bounds the result:

  interpret_lift_answer(user, ask, answer) → [{exercise, weight, reps}] with weights 20–900,
    reps 1–30, a max → reps 1, exercises that map to an anchor slug; or no_number; or None
    (model unavailable / unparseable → the caller's regex runs as before).
  review_targets(user, formula) → {calories, protein, reason} inside
    [TARGETS_REVIEW_MIN_RATIO, TARGETS_REVIEW_MAX_RATIO] × the formula and never under the
    calculator's floors; applied only when it moves ≥ TARGETS_REVIEW_MIN_DELTA_PCT or ≥ 10 g
    protein. The formula is the default; the review can only lower a number into the band or
    nudge it up ≤10%.

Flags PENDING_CARD_INTERPRET_ENABLED / TARGETS_MODEL_REVIEW_ENABLED (default on). Every
failure → None: the old path runs, nothing is blocked on a model call.
"""

from __future__ import annotations

import json
import logging
import re

import config

logger = logging.getLogger("cued.interpret")

LIFT_SITE = "interpret.lift_answer"
TARGETS_SITE = "interpret.targets_review"


def _call_json(user_id, site: str, prompt: str, *, model: str, max_tokens: int = 500) -> dict | None:
    try:
        from llm_client import make_client
        from agent_loop import _join_text
        from cost_tracking import track
        client = make_client()
        # (`temperature` is deprecated on current models; consistency comes from decisive rules
        # in the prompt and the code bounds on what comes back.)
        resp = client.messages.create(model=model, max_tokens=max_tokens,
                                      messages=[{"role": "user", "content": prompt}])
        try:
            track(user_id, site, model, resp)
        except Exception as e:  # noqa: BLE001
            logger.warning("INTERPRET_COST_TRACK_FAILED site=%s err=%s", site, e)
        raw = (_join_text(resp.content) or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            logger.info("INTERPRET_NO_JSON site=%s user=%s stop=%s raw=%r", site, user_id,
                        getattr(resp, "stop_reason", None), raw[:200])
            return None
        return data
    except Exception as e:  # noqa: BLE001 — never block the turn on an interpretation
        logger.warning("INTERPRET_CALL_FAILED site=%s user=%s err=%s", site, user_id, e)
        return None


# ─── the first-card answer ───────────────────────────────────────────────────────

_LIFT_PROMPT = """You are reading ONE text message from a gym user answering their coach's question about what they lift, so the coach can set the starting weights on their first workout card. Read it the way an experienced coach would.

THE COACH ASKED: "{ask}"
THEIR LEVEL: {level}
THEY REPLIED: "{answer}"

Return ONLY a JSON object:
{{"lifts": [{{"exercise": "<bench|squat|deadlift|ohp|row|rdl|leg press|incline|pulldown|curl|leg curl|leg extension>", "weight": <lb, number>, "reps": <int or null>, "is_max": <true|false>}}],
 "no_number": <true|false>, "confidence": <0-1>}}

Rules:
- "heaviest", "max", "PR", "all time", "most I've done", "once" → is_max true. A number given to a question about the HEAVIEST they've done is a max unless they say it's what they do for reps.
- "I do X for N", "sets of", "usually", "working weight" → reps N, is_max false. If they give both a max and a working set for the same lift, return BOTH (the working set wins later).
- A number with no reps and no max language, from a beginner answering a "heaviest" question → is_max true; from a trained lifter answering "what do u do for 5" → reps 5.
- "no clue", "never lifted", "just start light", "idk", "the bar" with no number → no_number true, lifts [].
- "just the bar" / "the empty bar" = 45 lb. Dumbbell numbers are per hand; keep them as said.
- Hedges ("i think", "like", "maybe", "around") lower confidence but don't drop the number. A goal ("wanna bench 225") is NOT a lift — leave it out.
- Kilograms → convert to lb. Never invent a number that isn't in the text."""

_LEVELS = {"intermediate": "intermediate (trains regularly)", "advanced": "advanced (years of training)",
           "beginner": "beginner (a little lifting)", "none": "never trained", "": "unknown (treat as new)"}
_ANCHOR_WORDS = re.compile(r"\b(bench|squat|deadlift|dl|ohp|overhead|shoulder press|military|row|rdl|leg press|incline|"
                           r"pulldown|pull down|curl|leg curl|leg ext|press)\b", re.I)


def interpret_lift_answer(user, ask: str, answer: str) -> dict | None:
    """→ {"items": [{"exercise","weight","reps"}], "no_number": bool, "confidence": float} or None."""
    if not getattr(config, "PENDING_CARD_INTERPRET_ENABLED", True):
        return None
    text = (answer or "").strip()
    if not text:
        return None
    # Nothing to interpret: no digit and no lift word → the regexes' no-number path decides.
    if not re.search(r"\d", text) and not _ANCHOR_WORDS.search(text) and not re.search(r"\bbar\b", text, re.I):
        return None
    lvl = (getattr(user, "experience", None) or "").strip().lower()
    prompt = _LIFT_PROMPT.format(ask=(ask or "").replace('"', "'")[:300], level=_LEVELS.get(lvl, _LEVELS[""]),
                                 answer=text.replace('"', "'")[:600])
    data = _call_json(getattr(user, "id", None), LIFT_SITE,
                      prompt, model=getattr(config, "INTERPRET_MODEL", config.ONBOARDING_EXTRACTOR_MODEL), max_tokens=900)
    if data is None:
        return None
    from workouts.calibrate import anchor_slug
    items: list[dict] = []
    for it in data.get("lifts") or []:
        if not isinstance(it, dict):
            continue
        ex = str(it.get("exercise") or "").strip()
        if not ex or not anchor_slug(ex):
            continue
        try:
            w = float(it.get("weight"))
        except (TypeError, ValueError):
            continue
        if not (20 <= w <= 900):
            continue
        reps = it.get("reps")
        try:
            reps = int(reps) if reps not in (None, "") else None
        except (TypeError, ValueError):
            reps = None
        if it.get("is_max"):
            reps = 1
        if reps is not None and not (1 <= reps <= 30):
            reps = None
        item = {"exercise": ex, "weight": w}
        if reps is not None:
            item["reps"] = reps
        items.append(item)
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    out = {"items": items, "no_number": bool(data.get("no_number")) and not items, "confidence": conf}
    logger.info("LIFT_ANSWER_INTERPRETED user=%s items=%s no_number=%s conf=%.2f text=%r",
                getattr(user, "id", None), [(i["exercise"], i["weight"], i.get("reps")) for i in items],
                out["no_number"], conf, text[:80])
    return out


# ─── the first targets ───────────────────────────────────────────────────────────

_TARGETS_PROMPT = """You are an experienced nutrition coach checking the day-one calorie and protein targets a formula produced for a new client, before the coach texts them. The formula (Mifflin-St Jeor → activity multiplier → goal adjustment) is usually right for average adults and wrong in known ways: it over-feeds heavy or sedentary people on a cut, trusts a "very active" label too much, and ignores what they actually eat today and what they can afford.

CLIENT
{profile}

WHAT THE FORMULA PRODUCED
- calories: {calories} (maintenance estimate {tdee}, BMR {bmr}, goal rule {goal_label} {goal_pct:+.0%}{limits}{ramp})
- protein: {protein} g

Would you hand this person these numbers on day one? Return ONLY a JSON object:
{{"calories": <int>, "protein": <int>, "reason": "<one plain sentence, max 160 chars, for the coach's eyes>", "confidence": <0-1>}}

Rules:
- If the formula is reasonable for THIS person, return it unchanged (same numbers) — do not fiddle.
- Fat loss at BMI 30+: the formula's deficit is too small for day one. Set calories around 0.75–0.85 × the maintenance estimate (a real, eatable deficit), lower still at BMI 40+ or with a desk job. Do this every time; it is the most common formula miss.
- A teenager cutting: do not hand them more than ~2500 unless they are genuinely very active (a sport they PLAY most days — managing a team is not that).
- Also lower when they say they eat far less today than the formula, or are on a tight budget / fixed meals: a day-one target should be something they can actually eat tomorrow. When in doubt on a cut, go lower; the coach adjusts up from the scale.
- Never go above the formula by more than 10%. Never below 1200 (women) / 1500 (men). Protein roughly 0.7–1.0 g per lb of lean-ish reference weight; don't push protein up when you lower calories.
- Numbers to the nearest 50 cal / 5 g."""


def _profile_lines(user) -> str:
    def _g(a, d="?"):
        v = getattr(user, a, None)
        return v if v not in (None, "") else d
    h = f"{_g('height_ft', '?')}'{_g('height_in', 0)}"
    w = _g("weight_lbs")
    bmi = None
    try:
        hin = int(getattr(user, "height_ft", 0) or 0) * 12 + int(getattr(user, "height_in", 0) or 0)
        if hin and w:
            bmi = round(703 * float(w) / (hin * hin), 1)
    except (TypeError, ValueError):
        bmi = None
    lines = [
        f"- age {_g('age')}, {_g('gender')}, {h}, {w} lb" + (f" (BMI {bmi})" if bmi else ""),
        f"- goal: {_g('goal')}; says the hard part is: {_g('biggest_obstacle')}",
        f"- experience: {_g('experience')}; trains {_g('workout_days')} days/week; equipment: {_g('equipment')}",
        f"- day-to-day activity: {_g('activity_level')}; steps/day: {_g('avg_steps')}; job/school: {_g('occupation')}",
        f"- eats now: about {_g('reported_intake')} cal/day (their own estimate; 0 or ? = unknown); food situation: {_g('cooking_situation')}",
    ]
    fc = (getattr(user, "food_context", None) or "").strip()
    if fc:
        lines.append(f"- food context: {fc[:200]}")
    return "\n".join(lines)


def review_targets(user, formula: dict, *, session=None) -> dict | None:
    """→ {"calories","protein","reason","confidence"} — bounded, or None when the formula stands."""
    if not getattr(config, "TARGETS_MODEL_REVIEW_ENABLED", True):
        return None
    try:
        f_cal, f_pro = int(formula["calories"]), int(formula["protein"])
    except (KeyError, TypeError, ValueError):
        return None
    limits = f", limits {', '.join(formula['goal_limits'])}" if formula.get("goal_limits") else ""
    ramp = formula.get("ramp")
    ramp_s = (f"; ramped from their stated ~{ramp['from_intake']} cal intake (full target {ramp['full_calories']})"
              if ramp else "")
    prompt = _TARGETS_PROMPT.format(profile=_profile_lines(user), calories=f_cal, tdee=formula.get("tdee", "?"),
                                    bmr=formula.get("bmr", "?"), goal_label=formula.get("goal_label", "?"),
                                    goal_pct=float(formula.get("goal_pct") or 0.0), limits=limits, ramp=ramp_s,
                                    protein=f_pro)
    # Two reads, the lower one wins: a day-one number on a cut should err conservative (every
    # live hand-fix went DOWN), and two samples remove most of the run-to-run variance.
    n = max(1, int(getattr(config, "TARGETS_REVIEW_SAMPLES", 2)))
    reads: list[dict] = []
    for _ in range(n):
        data = _call_json(getattr(user, "id", None), TARGETS_SITE, prompt,
                          model=getattr(config, "INTERPRET_MODEL", config.ONBOARDING_EXTRACTOR_MODEL), max_tokens=900)
        if not data:
            logger.info("TARGETS_REVIEW_NO_DATA user=%s", getattr(user, "id", None))
            continue
        try:
            reads.append({"calories": int(round(float(data.get("calories")))), "protein": int(round(float(data.get("protein")))),
                          "reason": data.get("reason"), "confidence": data.get("confidence")})
        except (TypeError, ValueError):
            logger.info("TARGETS_REVIEW_BAD_SHAPE user=%s data=%r", getattr(user, "id", None), str(data)[:160])
    if not reads:
        return None
    data = min(reads, key=lambda r: r["calories"])
    cal, pro = data["calories"], data["protein"]
    from macro_calculator import CALORIE_FLOOR, PROTEIN_FLOOR
    lo = float(getattr(config, "TARGETS_REVIEW_MIN_RATIO", 0.6))
    hi = float(getattr(config, "TARGETS_REVIEW_MAX_RATIO", 1.10))
    sex_floor = 1200 if (getattr(user, "gender", None) or "") == "female" else 1500
    floor = max(CALORIE_FLOOR, sex_floor)
    cal = max(floor, int(round(max(f_cal * lo, min(f_cal * hi, cal)) / 50.0) * 50))
    pro = max(PROTEIN_FLOOR, int(round(max(f_pro * 0.8, min(f_pro * 1.15, pro)) / 5.0) * 5))
    min_pct = float(getattr(config, "TARGETS_REVIEW_MIN_DELTA_PCT", 5)) / 100.0
    if abs(cal - f_cal) < f_cal * min_pct and abs(pro - f_pro) < 10:
        logger.info("TARGETS_REVIEW_KEPT user=%s formula=%s/%s model=%s/%s", getattr(user, "id", None), f_cal, f_pro,
                    data.get("calories"), data.get("protein"))
        return None
    reason = " ".join(str(data.get("reason") or "").split())[:160]
    out = {"calories": cal, "protein": pro, "reason": reason, "confidence": float(data.get("confidence") or 0.5)
           if isinstance(data.get("confidence"), (int, float)) else 0.5}
    logger.info("TARGETS_REVIEWED user=%s formula=%s/%s -> %s/%s (model said %s/%s) reason=%r",
                getattr(user, "id", None), f_cal, f_pro, cal, pro, data.get("calories"), data.get("protein"), reason)
    return out


def with_review(user, formula: dict, *, session=None) -> dict:
    """The formula dict with the review applied when it moved the numbers; `review` carries the
    old pair and the reason. Never raises."""
    try:
        r = review_targets(user, formula, session=session)
    except Exception as e:  # noqa: BLE001
        logger.warning("TARGETS_REVIEW_FAILED user=%s err=%s", getattr(user, "id", None), e)
        r = None
    if not r:
        return formula
    return dict(formula, calories=r["calories"], protein=r["protein"],
                review={"from": (formula["calories"], formula["protein"]), "reason": r["reason"]})

"""
Coaching lessons — the coach's SELF-authored notes about its own past misses with
THIS user. The second general-agent primitive from the college-assistant roadmap.
===========================================================================

What was there: memory stores facts ABOUT the user (user_profile_memory);
delivered_coaching_points stores what the coach EXPLAINED; nudge_guard stores what it
nudged TODAY. Nothing stored "I got this wrong with them before — don't repeat it."
Every lesson in the incident log (pork logged as chicken from a photo, the protein
nudge restated five times in a day, the week rundown that dropped Friday's deadline)
was learned by a human shipping a PR. The system never learned from its own turns.

What it becomes: a correction by the user → a lesson, phrased as an instruction to the
coach, generalized past the one incident, stored in the `coaching_lessons` memory
category and rendered as its own authoritative block (reactive loop AND heartbeat).

Division of labor (ENGINEERING_PLAYBOOK §I):
  code owns   — the trigger gate (correction cues), the storage (apply_facts' dedup
                ladder, ids, history, its own soft cap), hygiene (never safety, never
                stale-closed, pronouns neutralized, length/fragment floor), rendering,
                and the per-turn budget (one Haiku call, only on a cue hit)
  model owns  — phrasing the lesson and judging whether the correction GENERALIZES
                (a one-off data fix is handled by manage_log and is NOT a lesson)

Two writers, one ladder: the reactive model can write a lesson in-turn via
remember(category="coaching_lessons"); this module's background extractor catches the
corrections the model only apologized for. Both converge on memory.apply_facts.

Flags: LESSONS_ENABLED (category writes + render), LESSONS_EXTRACT_ENABLED (the
background extractor). Both default off; flip after the tier-2 gate.
"""

from __future__ import annotations

import json
import logging
import re

import config

logger = logging.getLogger("cued.lessons")

LESSONS_CATEGORY = "coaching_lessons"
LESSONS_MODEL = "claude-haiku-4-5-20251001"

BLOCK_HEADER = (
    "## LESSONS FROM COACHING THEM\n"
    "Your own past misses with this user, written by you. These are authoritative — "
    "follow them before any general habit. If they tell you one no longer applies, "
    "invalidate it by id (remember action=invalidate)."
)

# ─── the cue gate ───────────────────────────────────────────────────────────
# A BUDGET gate, not a classifier: a hit costs one Haiku call; a miss costs nothing.
# Precision-biased on the shapes real corrections take ("no that was X", "you already
# said", "stop asking", "i said", "not what i meant", "wrong"), word-boundary anchored
# so "wrong turn" / "not sure" don't fire.

_CUE_PATTERNS = [
    r"^\s*(no|nope|nah)\b[\s,.!-]*(that|it|this|i|you|we|the)\b",   # "no that was pork", "no i said…"
    r"\bthat'?s (wrong|not right|not what|incorrect)\b",
    r"\b(you'?re|your|that is) (wrong|incorrect|off)\b",
    r"^\s*wrong\b",
    r"\bnot what i (meant|said|asked|wanted)\b",
    r"\bi (said|told you|meant|asked for|didn'?t (say|eat|have|do|ask))\b",
    r"\byou (already|just) (said|told|asked|mentioned|logged)\b",
    r"\byou keep (saying|asking|telling|doing|logging)\b",
    r"\b(same thing|again and again|for the (second|third|fourth|fifth|\d+(st|nd|rd|th)) time)\b",
    r"\bstop (asking|saying|telling|repeating|bringing)\b",
    r"\bthat was (yesterday|earlier|last (night|week)|not|a different)\b",
    r"\bit was \w+( \w+)? not \w+\b",            # "it was oat milk not whole milk"
    r"\bwas \w+ not \w+\b",                     # "was pork not chicken"
    r"\b(fix|change|correct) (it|that|this)\b",
    r"\byou (missed|forgot|dropped|skipped|left out)\b",
    r"\bnot (pork|chicken|beef|yesterday|today|friday|thursday|monday|tuesday|wednesday|saturday|sunday)\b.*\b(i said|it was|that was)\b",
]
_CUE_RE = re.compile("|".join(f"(?:{p})" for p in _CUE_PATTERNS), re.IGNORECASE)


def looks_like_correction(text) -> bool:
    if not text or not isinstance(text, str):
        return False
    return bool(_CUE_RE.search(text))


# ─── the sanitizer (deterministic floor under whatever the model returns) ───

LESSON_MIN_WORDS = 4


def sanitize_lesson(text) -> str | None:
    """Strip quotes/bullets, cap length, require ≥4 words, neutralize pronouns, no URLs.
    Returns the clean lesson or None."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip().strip("\"'“”‘’").strip()
    t = re.sub(r"^[-•*\d.)\s]+", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    if not t or "http://" in t or "https://" in t:
        return None
    if len(t) > config.LESSONS_MAX_LEN:
        # A lesson longer than the cap is a paragraph, not a lesson — reject rather
        # than truncate mid-instruction (a cut instruction can invert its meaning).
        return None
    words = [w for w in re.split(r"\s+", t) if re.search(r"[A-Za-z0-9]", w)]
    if len(words) < LESSON_MIN_WORDS:
        return None
    try:
        from memory import neutralize_pronouns
        t = neutralize_pronouns(t)          # his/her/him/-self (memory.py leaves he/she alone)
    except Exception:  # pragma: no cover
        pass
    return _neutralize_subject_pronouns(t)


# memory.py's neutralizer skips he/she on purpose (facts have no subject). Lessons DO
# have a subject ("he finds it nagging"), and the prompt forbids gendered pronouns, so
# the floor rewrites he/she → they and repairs the common 3rd-person verb agreement.
_IRREGULAR = {"is": "are", "was": "were", "has": "have", "does": "do", "goes": "go",
              "doesn't": "don't", "isn't": "aren't", "wasn't": "weren't", "hasn't": "haven't"}
_SUBJ_RE = re.compile(r"\b(he|she)\b(\s+)(\w+(?:'t)?)", re.IGNORECASE)
_SUBJ_BARE_RE = re.compile(r"\b(he|she)\b", re.IGNORECASE)


def _neutralize_subject_pronouns(text: str) -> str:
    def _fix(m):
        verb = m.group(3)
        low = verb.lower()
        if low in _IRREGULAR:
            fixed = _IRREGULAR[low]
        elif re.fullmatch(r"[a-z]+ies", low) and len(low) > 4:      # tries → try
            fixed = low[:-3] + "y"
        elif re.fullmatch(r"[a-z]+(ches|shes|sses|xes|zes)", low):    # watches → watch
            fixed = low[:-2]
        elif re.fullmatch(r"[a-z]+s", low) and not low.endswith("ss") and len(low) > 3:
            fixed = low[:-1]                                           # finds → find
        else:
            fixed = verb
        if verb[:1].isupper():
            fixed = fixed[:1].upper() + fixed[1:]
        they = "They" if m.group(1)[:1].isupper() else "they"
        return f"{they}{m.group(2)}{fixed}"
    out = _SUBJ_RE.sub(_fix, text or "")
    return _SUBJ_BARE_RE.sub(lambda m: "They" if m.group(1)[:1].isupper() else "they", out)


# ─── the extractor ──────────────────────────────────────────────────────────

_PROMPT = """You are reviewing one exchange between a text-message coach and a user, looking for a LESSON the coach should carry forward about how to coach THIS user.

A lesson exists only when the user CORRECTED the coach — on a fact the coach got wrong, a thing it did that annoyed them, something it repeated, forgot, dropped, or misread — AND the correction generalizes to future turns.

Write the lesson as a short INSTRUCTION TO THE COACH (imperative, under {max_len} characters, no names, no gendered pronouns — use "they/them"). Generalize past the single incident:
  "no that was pork not chicken"           → "Verify the meat type in a food photo before logging it; ask when it's ambiguous."
  "you already said that five times"       → "Don't restate the same nudge more than once a day; vary the angle or let it rest."
  "you dropped friday, there's a deadline" → "When they ask for the rest of the week, list every day through Friday, deadlines first."
  "stop asking what i ate, you logged it"  → "Check the log before asking what they ate; never ask about something already logged."

NOT a lesson (return lesson = null):
  - a one-off data fix with nothing to generalize ("it was 2 eggs not 3" with no pattern claimed)
  - a new fact about the user ("i'm actually 21", "i switched to oat milk") — that's a memory fact, not a lesson
  - disagreement with advice that the coach should hold ("i don't want to sleep yet")
  - the user being wrong

The coach's message BEFORE the correction:
\"\"\"{coach_prior}\"\"\"

The user's message (the correction):
\"\"\"{user_message}\"\"\"

The coach's reply to it:
\"\"\"{coach_response}\"\"\"

Return ONLY valid JSON:
{{"lesson": "<instruction or null>", "generalizable": true|false, "confidence": "high"|"low"}}
Use "high" only when the correction is explicit and the generalization is obvious."""


def extract_lesson(user_message: str, coach_prior: str, coach_response: str, *,
                   user_id=None, client=None) -> str | None:
    """One Haiku call → a sanitized lesson or None. Never raises."""
    try:
        from llm_client import make_client
        client = client or make_client()
        prompt = _PROMPT.format(max_len=config.LESSONS_MAX_LEN,
                                coach_prior=(coach_prior or "(none)")[:1500],
                                user_message=(user_message or "")[:1500],
                                coach_response=(coach_response or "(none)")[:1500])
        resp = client.messages.create(model=LESSONS_MODEL, max_tokens=300,
                                      messages=[{"role": "user", "content": prompt}])
        try:
            from cost_tracking import track
            track(user_id, "lessons.extract", LESSONS_MODEL, resp)
        except Exception:  # pragma: no cover — cost tracking must never block a lesson
            pass
        raw = (resp.content[0].text or "").strip().replace("```json", "").replace("```", "").strip()
        if "{" in raw and "}" in raw:
            raw = raw[raw.index("{"):raw.rindex("}") + 1]
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if str(data.get("confidence", "")).lower() != "high" or not data.get("generalizable"):
            logger.info("LESSON_SKIPPED user=%s reason=gate confidence=%r generalizable=%r",
                        user_id, data.get("confidence"), data.get("generalizable"))
            return None
        lesson = sanitize_lesson(data.get("lesson"))
        if not lesson:
            logger.info("LESSON_SKIPPED user=%s reason=sanitize raw=%r", user_id, str(data.get("lesson"))[:120])
        return lesson
    except Exception as e:  # noqa: BLE001 — a background learner must never raise
        logger.warning("LESSON_EXTRACT_FAILED user=%s err=%s", user_id, e.__class__.__name__)
        return None


def _prior_coach_message(user_id: int, coach_response: str) -> str:
    """The coach's most recent OUTBOUND before the reply to the correction. The reply
    itself is usually already persisted when this runs, so skip a body equal to it."""
    from models import get_session, Message
    s = get_session()
    try:
        rows = (s.query(Message).filter(Message.user_id == user_id, Message.direction == "out")
                .order_by(Message.created_at.desc(), Message.id.desc()).limit(4).all())
    finally:
        s.close()
    for m in rows:
        if (m.body or "").strip() and (m.body or "").strip() != (coach_response or "").strip():
            return m.body
    return ""


FRESH_LESSON_WINDOW_S = 180


def _lesson_written_recently(user_id: int, *, now=None) -> bool:
    """True when any coaching_lessons entry's ts is within FRESH_LESSON_WINDOW_S."""
    from datetime import datetime, timezone
    from models import get_session, User
    now = now or datetime.now(timezone.utc)
    s = get_session()
    try:
        u = s.get(User, user_id)
        entries = ((u.user_profile_memory or {}).get(LESSONS_CATEGORY) or []) if u else []
    finally:
        s.close()
    for e in entries:
        ts = e.get("ts")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if 0 <= (now - t).total_seconds() <= FRESH_LESSON_WINDOW_S:
            return True
    return False


def store_lesson(user_id: int, lesson: str) -> dict | None:
    """Write one lesson through apply_facts under the row lock. Returns stats or None."""
    from models import get_session, User
    from memory import apply_facts
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not user:
            return None
        profile = dict(user.user_profile_memory or {})
        new_profile, stats = apply_facts(
            profile, [{"action": "add", "category": LESSONS_CATEGORY, "text": lesson}], user_id=user_id)
        user.user_profile_memory = new_profile
        flag_modified(user, "user_profile_memory")
        session.commit()
        return stats
    finally:
        session.close()


def extract_and_store_lesson_task(user_id: int, user_message: str, coach_response: str) -> None:
    """Background daemon task (spawned next to the other post-turn extractors).
    Gate (flag, cue) → one Haiku call → sanitize → apply_facts. Never raises."""
    try:
        if not (config.LESSONS_ENABLED and config.LESSONS_EXTRACT_ENABLED):
            return
        if not looks_like_correction(user_message):
            return
        if _lesson_written_recently(user_id):
            # The reactive model already saved a lesson this turn (remember tool). Two
            # writers converge on the dedup ladder for near-identical text, but a
            # paraphrase slips past Jaccard (live 2026-10-05: both stored one) — so the
            # background writer yields when a lesson is fresh.
            logger.info("LESSON_SKIPPED user=%s reason=fresh_lesson_this_turn", user_id)
            return
        prior = _prior_coach_message(user_id, coach_response)
        lesson = extract_lesson(user_message, prior, coach_response, user_id=user_id)
        if not lesson:
            return
        stats = store_lesson(user_id, lesson)
        logger.info("LESSON_STORED user=%s text=%r stats=%s", user_id, lesson, stats)
    except Exception as e:  # noqa: BLE001
        logger.warning("LESSON_TASK_FAILED user=%s err=%s", user_id, e.__class__.__name__)


# ─── rendering ──────────────────────────────────────────────────────────────

def lessons_block(user) -> tuple[str | None, list]:
    """(block_text, rendered_ids) for the loop/heartbeat context; (None, []) when the
    flag is off or there are no lessons. Ids are always shown — invalidation needs them."""
    if not config.LESSONS_ENABLED:
        return None, []
    profile = getattr(user, "user_profile_memory", None) or {}
    entries = [e for e in (profile.get(LESSONS_CATEGORY) or []) if e.get("text")]
    if not entries:
        return None, []
    lines = [f"- {e['text']} [id:{e.get('id')}]" for e in entries]
    return BLOCK_HEADER + "\n" + "\n".join(lines), [e.get("id") for e in entries]

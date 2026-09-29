"""
Exercise demo video library — a one-time form link the coach can drop inline.
============================================================================

Every entry points at ONE compilation video (https://youtu.be/S6rqpxVGKZ4) with a
per-movement `?t=` timestamp that jumps straight to that exercise's segment. When the
coach programs / discusses a movement the user hasn't seen a demo for yet, it may share
the link once; we then mark it seen (users.seen_exercise_demos) so it never repeats.

Originally shipped in the legacy multi-agent pipeline (agents/training.py, commit
ee939db) and lost as collateral when that pipeline was deleted. This is a clean port
into the single-agent architecture: a small, pure resolver (`unseen_demos_for`) that
context assembly calls, plus a background writer (`mark_demos_seen`) mirroring the
other modules' fail-open, flag-gated shape.

Everything is fail-open: with EXERCISE_DEMOS_ENABLED off (or on any error) the caller
injects nothing and the turn is unaffected. The resolver NEVER writes to the DB, so it
is safe to call from context assembly and from tests.
"""

from __future__ import annotations

import logging
import re
import threading

import config

logger = logging.getLogger("cued.exercise_demos")

# movement slug → timestamped segment of the ONE compilation video. Ported verbatim
# from agents/training.py @ ee939db ("phase 7: exercise demo video library").
EXERCISE_DEMO_LINKS = {
    "machine_pec_deck": "https://youtu.be/S6rqpxVGKZ4?t=262",
    "weighted_dips": "https://youtu.be/S6rqpxVGKZ4?t=340",
    "bench_press": "https://youtu.be/S6rqpxVGKZ4?t=592",
    "overhead_cable_triceps_extension": "https://youtu.be/S6rqpxVGKZ4?t=872",
    "incline_bench_press": "https://youtu.be/S6rqpxVGKZ4?t=1109",
    "machine_lat_pullover": "https://youtu.be/S6rqpxVGKZ4?t=38",
    "bayesian_cable_curl": "https://youtu.be/S6rqpxVGKZ4?t=406",
    "preacher_curl": "https://youtu.be/S6rqpxVGKZ4?t=906",
    "chest_supported_t_bar_row": "https://youtu.be/S6rqpxVGKZ4?t=998",
    "pull_up": "https://youtu.be/S6rqpxVGKZ4?t=1172",
    "dumbbell_shrugs": "https://youtu.be/S6rqpxVGKZ4?t=74",
    "reverse_pec_deck": "https://youtu.be/S6rqpxVGKZ4?t=307",
    "overhead_press": "https://youtu.be/S6rqpxVGKZ4?t=506",
    "lateral_raise": "https://youtu.be/S6rqpxVGKZ4?t=947",
    "standing_calf_raise": "https://youtu.be/S6rqpxVGKZ4?t=100",
    "nautilus_glute_drive": "https://youtu.be/S6rqpxVGKZ4?t=374",
    "walking_lunge": "https://youtu.be/S6rqpxVGKZ4?t=543",
    "seated_leg_curl": "https://youtu.be/S6rqpxVGKZ4?t=770",
    "leg_extension": "https://youtu.be/S6rqpxVGKZ4?t=819",
    "romanian_deadlift": "https://youtu.be/S6rqpxVGKZ4?t=1060",
    "squat": "https://youtu.be/S6rqpxVGKZ4?t=1252",
    "dumbbell_wrist_curls_and_extensions": "https://youtu.be/S6rqpxVGKZ4?t=125",
    "neck_curls_and_extensions": "https://youtu.be/S6rqpxVGKZ4?t=172",
    "cable_crunch": "https://youtu.be/S6rqpxVGKZ4?t=224",
    "deadlift": "https://youtu.be/S6rqpxVGKZ4?t=506",
}

# Real-world names / slugs the card and conversation use → the canonical key above.
# CONSERVATIVE by design: an alias must be the SAME movement. A missing demo is fine;
# a WRONG-movement demo is a correctness bug. So we deliberately DO NOT map, e.g.,
# "lat pulldown" (a vertical pull) to machine_lat_pullover (a straight-arm pullover),
# "barbell curl" / "bicep curl" to any curl entry (bayesian_cable_curl and preacher_curl
# are specific variations — there is no generic curl), or "back extension" /
# "hyperextension", "seated calf raise", "lying leg curl", generic "t-bar row" — those
# have no genuinely-matching canonical key, so they MISS. Keys here are normalized at
# module load (same normalize as the input), so spacing/hyphen/case don't matter.
_ALIASES = {
    # pull_up — bodyweight vertical pull (chin-up is the supinated-grip same movement)
    "pullup": "pull_up",
    "pullups": "pull_up",
    "pull ups": "pull_up",
    "pull-ups": "pull_up",
    "chin up": "pull_up",
    "chinup": "pull_up",
    "chin ups": "pull_up",
    # romanian_deadlift
    "rdl": "romanian_deadlift",
    "rdls": "romanian_deadlift",
    "romanian dl": "romanian_deadlift",
    "romanian deadlifts": "romanian_deadlift",
    # overhead_press (military press is the same standing barbell press)
    "ohp": "overhead_press",
    "military press": "overhead_press",
    "overhead presses": "overhead_press",
    # squat (back / barbell squat = the standard squat in the demo)
    "squats": "squat",
    "back squat": "squat",
    "back squats": "squat",
    "barbell squat": "squat",
    # deadlift (conventional = the standard deadlift in the demo)
    "deadlifts": "deadlift",
    "conventional deadlift": "deadlift",
    # bench_press
    "flat bench": "bench_press",
    "flat bench press": "bench_press",
    "bench presses": "bench_press",
    # incline_bench_press
    "incline bench": "incline_bench_press",
    "incline press": "incline_bench_press",
    "incline bench presses": "incline_bench_press",
    # weighted_dips (dips are the same movement, just loaded)
    "dip": "weighted_dips",
    "dips": "weighted_dips",
    "weighted dip": "weighted_dips",
    "chest dips": "weighted_dips",
    # lateral_raise
    "lateral raises": "lateral_raise",
    "side lateral raise": "lateral_raise",
    "side lateral raises": "lateral_raise",
    "db lateral raise": "lateral_raise",
    "dumbbell lateral raise": "lateral_raise",
    # leg_extension
    "leg extensions": "leg_extension",
    "quad extension": "leg_extension",
    "quad extensions": "leg_extension",
    # seated_leg_curl (hamstring curl; keep to the seated machine we have a demo for)
    "seated leg curls": "seated_leg_curl",
    # preacher_curl
    "preacher curls": "preacher_curl",
    # cable_crunch
    "cable crunches": "cable_crunch",
    # walking_lunge
    "walking lunges": "walking_lunge",
    "walking lunge": "walking_lunge",
    "lunges": "walking_lunge",
    # standing_calf_raise (kept to STANDING — seated calf raise is a different emphasis)
    "standing calf raises": "standing_calf_raise",
    # machine_pec_deck
    "pec deck": "machine_pec_deck",
    "pec dec": "machine_pec_deck",
    "chest fly machine": "machine_pec_deck",
    # dumbbell_shrugs
    "db shrugs": "dumbbell_shrugs",
    "dumbbell shrug": "dumbbell_shrugs",
}


def _normalize(name: str) -> str:
    """Free-text exercise name → slug: lowercase, trim, runs of spaces/hyphens → '_'.
    Same rule as the original agents/training._normalize_exercise_name."""
    return re.sub(r"[\s\-]+", "_", (name or "").strip().lower())


# Alias keys, normalized once at load so lookup is a single normalized-key dict hit.
_ALIASES_NORM = {_normalize(k): v for k, v in _ALIASES.items()}


def _resolve(name: str) -> str | None:
    """An exercise name → its canonical demo key, or None if we have no SAME-movement
    demo for it. Direct key first, then the conservative alias table."""
    key = _normalize(name)
    if key in EXERCISE_DEMO_LINKS:
        return key
    return _ALIASES_NORM.get(key)


def unseen_demos_for(user, exercise_names, limit: int = 1) -> dict[str, str]:
    """{original_name: url} for up to `limit` (default 1) exercises the user hasn't been
    shown a demo for yet. PURE / read-only — NEVER writes to the DB, so it is safe from
    context assembly and tests. Fail-open: flag off or any error → {}.

    Resolution per name: normalize → direct key → alias. A name that resolves to a key
    already in user.seen_exercise_demos is skipped."""
    if not config.EXERCISE_DEMOS_ENABLED:
        return {}
    try:
        seen = getattr(user, "seen_exercise_demos", None) or {}
        result: dict[str, str] = {}
        chosen_keys: set[str] = set()
        for name in exercise_names or []:
            key = _resolve(name)
            if not key or key in chosen_keys or seen.get(key):
                continue
            result[name] = EXERCISE_DEMO_LINKS[key]
            chosen_keys.add(key)
            if len(result) >= max(1, limit):
                break
        return result
    except Exception:  # noqa: BLE001 — a form-demo hint must never break a turn
        logger.exception("EXERCISE_DEMOS_RESOLVE_FAILED")
        return {}


def mark_demos_seen(user_id: int, canonical_keys) -> None:
    """Mark `canonical_keys` seen for the user in a daemon thread (non-blocking), so the
    demo isn't offered again. Mirrors the original _mark_seen: reload the row, assign a
    NEW dict object (SQLAlchemy JSON change detection is by-identity), commit. Fail-open:
    off flag / empty keys / any error → no-op."""
    if not config.EXERCISE_DEMOS_ENABLED:
        return
    keys = [k for k in (canonical_keys or []) if k]
    if not keys:
        return

    def _mark_seen():
        from models import get_session, User
        session = get_session()
        try:
            user_row = session.get(User, user_id)
            if user_row:
                updated = dict(user_row.seen_exercise_demos or {})
                for k in keys:
                    updated[k] = True
                user_row.seen_exercise_demos = updated  # new object → JSON dirty
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("EXERCISE_DEMOS_MARK_SEEN_FAILED user=%s", user_id)
            session.rollback()
        finally:
            session.close()

    threading.Thread(target=_mark_seen, daemon=True).start()

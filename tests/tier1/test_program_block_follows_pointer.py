"""
The PROGRAM block's "today" follows the split pointer — the card builder's truth — not the
calendar slot alone.

Live 2026-10-28 (user 77, Alexey): program mon push · tue pull · wed legs · thu push …; he
trained PUSH on Tuesday (the calendar's pull slot), so the pointer said push (confirmed) and
the card builder's rule (workouts.start.infer_template: the day AFTER the pointer) gave pull
for Wednesday. The heartbeat only sees the PROGRAM block, which rendered "today (wed): legs
day" → the 7:29 briefing said "legs at 8". The reactive loop saw both blocks and reconciled
them as "u missed yesterday so it rolls forward" (he hadn't missed anything: "I did push
yesterday"). Three stories about one day. One rule now: when a pointer exists, today's label
is what the card would build, the calendar slot is named as the slot, and a day done out of
order is never a "missed" day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

import config

ALEXEY_PROGRAM = {
    "version": 1, "level": "beginner", "goal": "muscle_building,strength", "equipment": "full_gym",
    "days_per_week": 6, "split": "ppl", "cycle": ["push", "pull", "legs"],
    "days": {"mon": "push", "tue": "pull", "wed": "legs", "thu": "push", "fri": "pull", "sat": "legs"},
    "times": {d: "08:00" for d in ("mon", "tue", "wed", "thu", "fri", "sat")},
    "exercises": {"push": ["bench press"], "pull": ["barbell row"], "legs": ["squat"]},
    "scheme": {"sets": 3, "reps": "10-12", "why": "learning the movements"},
    "progression": {"rule": "double progression", "text": "hit every rep on every set → the weight goes up next time"},
    "cardio": None, "constraints": [], "days_confirmed": True,
}

WED = datetime(2026, 10, 28, 14, 29, tzinfo=timezone.utc)      # Wed 07:29 PT — the live briefing tick
THU = datetime(2026, 10, 29, 14, 29, tzinfo=timezone.utc)      # Thu 07:29 PT
SUN = datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)       # Sun noon PT


def _alexey(**over):
    base = dict(name="Alexey", user_timezone="America/Los_Angeles", current_split="ppl", split_days=None,
                confirmed_training_split=None, custom_templates=None, program=dict(ALEXEY_PROGRAM),
                split_pointer_day=None, split_pointer_at=None, split_pointer_source=None)
    base.update(over)
    return NS(**base)


def _ctx(user, now):
    from workouts.program import program_context
    return program_context(user, now=now)


# ─── the live case ────────────────────────────────────────────────────────────

def test_wednesday_after_an_out_of_order_push_is_pull_not_legs():
    """Pointer push (confirmed, Tue) → the card would build pull → the block says pull, names
    the calendar slot (legs), says the cycle rolls, and forbids 'u missed'."""
    u = _alexey(split_pointer_day="push", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 27, 19, 50))     # Tue 12:50 PT, naive UTC
    ctx = _ctx(u, WED)
    assert "- today (wed): pull day" in ctx, ctx
    assert "today (wed): legs day" not in ctx
    assert "calendar slot" in ctx and "legs" in ctx
    assert "push" in ctx and "tue" in ctx                       # what they actually did, and when
    assert "rolls" in ctx and "not a missed day" in ctx and "never say" in ctx
    assert "next: thu legs" in ctx, ctx                          # the roll carries into the next session


def test_the_heartbeat_sees_the_same_line_as_the_loop(db, monkeypatch):
    """The heartbeat has no SPLIT POINTER block of its own — it only reads the PROGRAM block,
    so the block itself must carry the pointer's answer. Live: the 7:29 heartbeat said
    'legs at 8' from 'today (wed): legs day'."""
    from tests.factories import make_user
    from heartbeat import _proactive_context
    user = make_user(db, name="Alexey", current_split="ppl", program=dict(ALEXEY_PROGRAM),
                     split_pointer_day="push", split_pointer_source="confirmed",
                     split_pointer_at=datetime(2026, 10, 27, 19, 50))
    import heartbeat
    monkeypatch.setattr(heartbeat, "_naive_utcnow", lambda: WED.replace(tzinfo=None), raising=False)
    import workouts.program as program
    real = program.program_context
    monkeypatch.setattr(program, "program_context", lambda u, prog=None, *, now=None: real(u, prog, now=WED))
    ctx = _proactive_context(user, db)
    assert "- today (wed): pull day" in ctx, ctx
    assert "today (wed): legs day" not in ctx


# ─── the unchanged paths ──────────────────────────────────────────────────────

def test_no_pointer_keeps_the_calendar_label():
    ctx = _ctx(_alexey(), WED)
    assert "- today (wed): legs day; next: thu push" in ctx, ctx
    assert "calendar slot" not in ctx and "missed" not in ctx


def test_pointer_that_agrees_with_the_calendar_renders_plainly():
    u = _alexey(split_pointer_day="pull", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 27, 19, 50))
    ctx = _ctx(u, WED)
    assert "- today (wed): legs day; next: thu push" in ctx, ctx
    assert "calendar slot" not in ctx and "missed" not in ctx


def test_rest_day_stays_a_rest_day_and_the_next_session_follows_the_pointer():
    """Sunday is off on the calendar either way; the next session (mon) is the day AFTER the
    pointer, not the calendar's mon push."""
    u = _alexey(split_pointer_day="push", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 24, 19, 50))
    ctx = _ctx(u, SUN)
    assert "- today (sun): rest day; next: mon pull" in ctx, ctx


def test_inferred_pointer_keeps_the_hedge():
    u = _alexey(split_pointer_day="push", split_pointer_source="inferred",
                split_pointer_at=datetime(2026, 10, 27, 19, 50))
    ctx = _ctx(u, WED)
    assert "- today (wed): pull day" in ctx
    assert "inferred" in ctx and "hedge" in ctx, ctx


def test_a_session_already_done_today_is_said_so():
    """The pointer moved TODAY (his home pull session closed at 00:20 Thu): today's session is
    done; the next session is the day after it, not 'legs today' or the calendar's push."""
    u = _alexey(split_pointer_day="pull", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 29, 7, 20))    # Thu 00:20 PT
    ctx = _ctx(u, THU)
    assert "- today (thu): pull day — done" in ctx, ctx
    assert "next: fri legs" in ctx, ctx
    assert "today (thu): push day" not in ctx


def test_pointer_outside_the_cycle_falls_back_to_the_calendar():
    u = _alexey(split_pointer_day="chest", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 27, 19, 50))
    ctx = _ctx(u, WED)
    assert "- today (wed): legs day; next: thu push" in ctx, ctx


def test_flag_off_is_the_old_block(monkeypatch):
    monkeypatch.setattr(config, "PROGRAM_BLOCK_FOLLOWS_POINTER_ENABLED", False, raising=False)
    u = _alexey(split_pointer_day="push", split_pointer_source="confirmed",
                split_pointer_at=datetime(2026, 10, 27, 19, 50))
    ctx = _ctx(u, WED)
    assert "- today (wed): legs day; next: thu push" in ctx, ctx


def test_flag_default_on():
    assert config.PROGRAM_BLOCK_FOLLOWS_POINTER_ENABLED is True


def test_block_and_card_builder_agree_on_every_weekday():
    """Property: for every weekday and every pointer position, a training day's label in the
    block equals what infer_template would build for the card."""
    from workouts.start import infer_template
    from workouts.program import DAYS
    for i, day in enumerate(DAYS):
        now = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc) + timedelta(days=i)   # Mon 10-26 … Sun 11-01, noon PT
        for ptr in ("push", "pull", "legs"):
            u = _alexey(split_pointer_day=ptr, split_pointer_source="confirmed",
                        split_pointer_at=datetime(2026, 10, 20, 19, 50))
            ctx = _ctx(u, now)
            if day in ALEXEY_PROGRAM["days"]:
                assert f"- today ({day}): {infer_template(u, now=now)} day" in ctx, (day, ptr, ctx)
            else:
                assert f"- today ({day}): rest day" in ctx, (day, ptr, ctx)

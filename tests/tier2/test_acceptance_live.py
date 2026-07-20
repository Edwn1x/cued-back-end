"""
Tier-2 acceptance cases — live model, LLM-judged. Excluded from CI (no funded
key in Actions); run locally with `pytest --run-tier2`. These need model
JUDGMENT (did the coach *use* the fact / name the right day / sound like one
person / actually delete the row), which a mocked LLM cannot provide.

Marked skip until the phase that makes them runnable lands; the @tier2 marker
keeps them out of the default/CI selection regardless.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


@pytest.mark.skip(reason="failure 2 — calendar screenshot -> events/schedule; "
                  "lands Phase 3 (read_image). Needs 5-10 real screenshots.")
def test_calendar_screenshot_produces_schedule_state():
    ...


@pytest.mark.skip(reason="failure 1 (judged) — model actually USES a fact stated "
                  "five turns earlier; lands Phase 2 (single loop, full context)")
def test_fact_is_used_five_turns_later():
    ...


@pytest.mark.skip(reason="failure 4 (judged) — model names the same split day on "
                  "two asks the same day; lands Phase 2")
def test_named_split_day_is_identical_across_two_asks():
    ...


@pytest.mark.skip(reason="failure 6 (judged) — replies + proactive messages read "
                  "as one consistent persona with warm accountability; lands Phase 2")
def test_voice_is_one_consistent_persona():
    ...


@pytest.mark.skip(reason="correction round-trip (full) — duplicate meal deleted "
                  "via manage_log, excluded from totals, never re-referenced; "
                  "lands Phase 3")
def test_duplicate_meal_correction_round_trip():
    ...

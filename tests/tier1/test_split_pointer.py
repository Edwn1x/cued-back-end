"""
Phase 1 — split pointer: two facts + provenance, code-mediated advancement.
Cycle mapping is write-time only; the pointer read returns raw facts (no
"today's workout" computation).
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("msg,expected", [
    ("just did legs", "legs"),
    ("push day done", "push"),
    ("hit lower today", "lower"),
    ("full body sesh", "full_body"),
    ("hit chest and arms", None),   # ambiguous across systems -> not guessed
    ("just got back from the gym", None),
])
def test_parse_named_split_day_is_precision_biased(msg, expected):
    from split_pointer import parse_named_split_day
    assert parse_named_split_day(msg) == expected


def test_named_day_is_confirmed(db):
    from tests.factories import make_user
    from split_pointer import advance_split_pointer

    user = make_user(db, current_split="ppl")
    p = advance_split_pointer(user.id, named_day="legs")
    assert p == {"day": "legs", "at": p["at"], "source": "confirmed"}


def test_unnamed_already_went_infers_next_day(db):
    from tests.factories import make_user
    from split_pointer import advance_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    p = advance_split_pointer(user.id, named_day=None)   # unnamed "already went"
    assert p["day"] == "pull" and p["source"] == "inferred"


def test_named_correction_overwrites_inferred_same_day(db):
    from tests.factories import make_user
    from split_pointer import advance_split_pointer, get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    advance_split_pointer(user.id, named_day=None)          # -> inferred pull
    advance_split_pointer(user.id, named_day="legs")        # correction: "actually I did legs"
    p = get_split_pointer(user.id)
    assert p["day"] == "legs" and p["source"] == "confirmed"


def test_second_unnamed_advance_same_day_is_a_noop(db):
    from tests.factories import make_user
    from split_pointer import advance_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    advance_split_pointer(user.id, named_day=None)          # -> pull
    p = advance_split_pointer(user.id, named_day=None)      # same day again -> no double advance
    assert p["day"] == "pull"


def test_unnamed_advance_never_guesses_without_a_current_day(db):
    from tests.factories import make_user
    from split_pointer import advance_split_pointer, get_split_pointer

    user = make_user(db, current_split="ppl")   # no pointer day yet
    advance_split_pointer(user.id, named_day=None)
    assert get_split_pointer(user.id) is None    # left unchanged, no guess


def test_inbound_gym_message_advances_pointer_end_to_end(db, driver):
    from tests.factories import make_user
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="pull")
    driver.send(user, "just got back from the gym")   # unnamed -> infer legs
    p = get_split_pointer(user.id)
    assert p["day"] == "legs" and p["source"] == "inferred"


def test_inbound_named_gym_message_confirms_the_day(db, driver):
    from tests.factories import make_user
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    driver.send(user, "just got back from the gym, did legs")   # named -> confirmed
    p = get_split_pointer(user.id)
    assert p["day"] == "legs" and p["source"] == "confirmed"

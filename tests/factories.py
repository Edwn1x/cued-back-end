"""
Fixture-user factory. Only `phone` and `name` are non-null on User; everything
else is nullable, so a fixture user is a post-onboarding shell with just enough
set to reach the normal (non-onboarding) inbound path.
"""

from __future__ import annotations

import itertools

_phone_counter = itertools.count(1)


def _template_anchors() -> dict:
    """users.lift_anchors that reproduce the global templates' loads exactly: a stated
    135×5 round-trips to 135 for 5, so a fixture with these gets the pre-calibration
    card numbers (mechanics tests) and is never asked for its lifts."""
    from workouts.templates import TEMPLATES, PART_TEMPLATES
    out = {}
    for exs in (*TEMPLATES.values(), *PART_TEMPLATES.values()):
        for e in exs:
            if e.default_weight and e.slug not in out:
                out[e.slug] = {"weight": e.default_weight, "reps": e.reps, "source": "test"}
    return out


TEMPLATE_ANCHORS = _template_anchors()


def make_user(session, **overrides):
    """Create + commit a post-onboarding User. Override any column via kwargs."""
    from models import User

    n = next(_phone_counter)
    defaults = dict(
        phone=f"+1555010{n:04d}",
        name="Test User",
        onboarding_step=3,            # >=3 == onboarding complete (past the onboarding branch)
        user_timezone="America/Los_Angeles",
        wake_time="07:00",
        sleep_time="23:00",
        experience="intermediate",
        equipment="full_gym",
        active=True,
    )
    defaults.update(overrides)

    user = User(**defaults)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user

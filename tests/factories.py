"""
Fixture-user factory. Only `phone` and `name` are non-null on User; everything
else is nullable, so a fixture user is a post-onboarding shell with just enough
set to reach the normal (non-onboarding) inbound path.
"""

from __future__ import annotations

import itertools

_phone_counter = itertools.count(1)


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

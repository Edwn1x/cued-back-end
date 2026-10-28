"""
Presence ledger for the retention page: one row per (user, local day, signal).

`messages` already records every inbound text, but a workout-card open, a tapback and a
stat-card view leave no row, and users.last_active_at keeps only the LATEST stamp — so
until this table a day where someone trained off the card and never typed was
unrecoverable. wake_model.touch_last_active (the one place every presence event already
passes through) calls `record` with the signal; nothing else writes here.

Fail-open and own session: a ledger write must never block or fail the request that
triggered it. Read by admin_engagement.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import ActivityDay, User, get_session

logger = logging.getLogger("cued.activity_days")

SIGNALS = ("inbound", "tapback", "card_open", "stat_card_view")
# The signals that mean "they came to us" without a text we could have prompted.
PRESENCE_SIGNALS = ("tapback", "card_open", "stat_card_view")
DEFAULT_TZ = "America/Los_Angeles"


def local_day(dt_naive_utc: datetime, tz_name: str | None) -> str:
    """A stored naive-UTC instant → YYYY-MM-DD in the user's zone (default zone when the
    name is missing or bad; silent — this runs on every inbound and the admin page)."""
    try:
        tz = ZoneInfo((tz_name or "").strip() or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo(DEFAULT_TZ)
    aware = dt_naive_utc if dt_naive_utc.tzinfo else dt_naive_utc.replace(tzinfo=timezone.utc)
    return aware.astimezone(tz).strftime("%Y-%m-%d")


def record(user_id: int, signal: str, *, at: datetime | None = None) -> None:
    """Upsert the (user, day, signal) row: first_at keeps the earliest, last_at the latest,
    count increments. Unknown signals are logged and dropped, never raised."""
    if not user_id:
        return
    if signal not in SIGNALS:
        logger.warning("ACTIVITY_DAY_BAD_SIGNAL user=%s signal=%r", user_id, signal)
        return
    when = at or datetime.now(timezone.utc).replace(tzinfo=None)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        s = get_session()
        try:
            tz_name = s.query(User.user_timezone).filter(User.id == user_id).scalar()
            day = local_day(when, tz_name)
            stmt = pg_insert(ActivityDay).values(user_id=user_id, day=day, signal=signal,
                                                 first_at=when, last_at=when, count=1)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_activity_days_user_day_signal",
                set_={"last_at": when, "count": ActivityDay.count + 1},
            )
            s.execute(stmt)
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("ACTIVITY_DAY_WRITE_FAILED user=%s signal=%s err=%s", user_id, signal, e)


__all__ = ["record", "local_day", "SIGNALS", "PRESENCE_SIGNALS"]

"""
One-time, READ-ONLY audit — post-burn-in item 3.

Events and dated `schedule`-category memory facts written before the burn-in-fixes
PR (naive-UTC render boundary + local-day windowing) may hold values 7h/one-day off
from what the *new* `timefmt` boundary would have produced. Consolidation merges and
closes stale facts; it does not re-derive their values, so a nightly pass will not
self-heal this. This script does not mutate anything — it prints every active `Event`
row rendered through the current (correct) `timefmt` boundary, next to its raw stored
value, so a human can eyeball a value that looks wrong (e.g. a "9am lecture" rendering
as 2am) and correct it conversationally or via `manage_log edit`. Dated `schedule`
facts live as free text inside `user_profile_memory` — not machine-parseable dates —
so they're only flagged for manual review, never auto-corrected.

Usage:
    python audit_timestamps.py            # every active user
    python audit_timestamps.py +15551234567 # one user, by phone
    python audit_timestamps.py 42           # one user, by id
"""

from __future__ import annotations

import sys

from models import get_session, User, Event, active
from timefmt import render_time, render_date


def _target_users(session, arg: str | None):
    if not arg:
        return session.query(User).filter(User.active.is_(True)).order_by(User.id).all()
    if arg.lstrip("+").isdigit() and arg.startswith("+"):
        user = session.query(User).filter(User.phone == arg).one_or_none()
    else:
        user = session.get(User, int(arg)) if arg.isdigit() else None
    return [user] if user else []


def audit_user(session, user: User) -> None:
    print(f"\n=== user {user.id} ({user.name!r}, {user.phone}, tz={user.user_timezone}) ===")

    events = (active(session, Event, user_id=user.id)
              .order_by(Event.occurred_at.asc()).all())
    if not events:
        print("  events: (none)")
    else:
        print(f"  events ({len(events)}):")
        for e in events:
            local_start = render_time(e.occurred_at, user, relative=False) if e.occurred_at else "(none)"
            local_end = render_time(e.ends_at, user, relative=False) if e.ends_at else None
            print(f"    [id {e.id}] {e.event_type} — {(e.raw_text or '').strip()[:60]!r}")
            print(f"        raw UTC:   occurred_at={e.occurred_at}  ends_at={e.ends_at}")
            print(f"        rendered:  {render_date(e.occurred_at, user)} {local_start}"
                  + (f" -> {local_end}" if local_end else ""))

    schedule_facts = (user.user_profile_memory or {}).get("schedule") or []
    dated = [f for f in schedule_facts if f.get("text")]
    if dated:
        print(f"  schedule-category memory facts ({len(dated)}) — REVIEW MANUALLY, free text, "
              f"not machine-correctable:")
        for f in dated:
            print(f"    [{f.get('id')}] {f.get('text')!r} (ts={f.get('ts')})")


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    session = get_session()
    try:
        users = _target_users(session, arg)
        if not users:
            print(f"no matching active user for {arg!r}")
            return
        for user in users:
            audit_user(session, user)
    finally:
        session.close()
    print("\nRead-only audit — no rows were modified. Correct wrong events via manage_log "
          "edit (conversationally or directly); correct wrong schedule facts by hand.")


if __name__ == "__main__":
    main()

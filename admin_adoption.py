"""Adoption metrics for the admin console (founder, 2026-10-13): are people connecting and
accepting what setup proposes? Read-only, from the tables the setup sequence already writes:

  calendar / bcourses / wearable — offered = the user's connect_offers ledger (or an offer /
      link message of that provider) · connected = a connected Integration row · time = first
      offer → connected
  water — offered = users.water_offer_status set · yes / no / ignored
  first card — asked = a card_setup message · built = users.card_setup_at · opened = card_opened_at
  onboarding — activated → completed, median turns / minutes

Every number is per activated user inside the window (default 30 days), so a new feature's
acceptance is readable the week it ships. Pure read; the page renders whatever this returns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func

from models import Integration, Message, User

CONNECT_PROVIDERS = (("gcal", "google calendar"), ("bcourses", "bcourses feed"), ("google_health", "fitbit / pixel"))


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):.0f}%" if den else "—"


def _minutes(deltas: list[float]) -> str:
    if not deltas:
        return "—"
    m = median(deltas)
    return f"{m:.0f} min" if m < 180 else f"{m / 60:.1f} h"


def _parse_iso(s):
    """The connect_offers ledger stores naive UTC isoformat; an aware value is normalised."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def adoption(session, *, days: int = 30, now: datetime | None = None) -> dict:
    now = now or _naive_utcnow()
    since = now - timedelta(days=days)
    users = (session.query(User)
             .filter(User.activated_at.isnot(None), User.activated_at >= since).all())
    uids = [u.id for u in users]
    completed = [u for u in users if (u.onboarding_step or 0) >= 3]
    out = {"days": days, "activated": len(users), "completed": len(completed),
           "completion_rate": _pct(len(completed), len(users)), "rows": []}
    if not uids:
        out["onboarding_minutes"] = "—"
        return out

    # onboarding duration: first onboarding outbound → completion
    first_out = dict(session.query(Message.user_id, func.min(Message.created_at))
                     .filter(Message.user_id.in_(uids), Message.direction == "out")
                     .group_by(Message.user_id).all())
    durations = [((u.onboarding_completed_at - first_out[u.id]).total_seconds() / 60)
                 for u in completed if u.onboarding_completed_at and u.id in first_out
                 and u.onboarding_completed_at > first_out[u.id]]
    out["onboarding_minutes"] = _minutes(durations)

    # connect steps
    integ = {}
    for r in session.query(Integration).filter(Integration.user_id.in_(uids)).all():
        integ.setdefault((r.user_id, r.provider), []).append(r)
    offer_msgs = {}
    for uid, mt, body, at in (session.query(Message.user_id, Message.message_type, Message.body, Message.created_at)
                              .filter(Message.user_id.in_(uids), Message.direction == "out",
                                      Message.message_type.in_(("connect_offer", "connect_link"))).all()):
        offer_msgs.setdefault(uid, []).append((mt, body or "", at))
    for provider, label in CONNECT_PROVIDERS:
        offered, connected, deltas, ignored = 0, 0, [], 0
        for u in users:
            ledger = (u.connect_offers or {}) if isinstance(u.connect_offers, dict) else {}
            first_at = _parse_iso(ledger.get(provider)) if ledger.get(provider) else None
            if first_at is None:
                hits = [at for mt, body, at in offer_msgs.get(u.id, [])
                        if (provider == "gcal" and ("calendar" in body or "/c/gcal/" in body))
                        or (provider == "bcourses" and "bcourses" in body)
                        or (provider == "google_health" and ("fitbit" in body or "pixel" in body or "/c/google_health/" in body))]
                first_at = min(hits) if hits else None
            rows = integ.get((u.id, provider), [])
            done = [r for r in rows if r.status == "connected"]
            if first_at is None and not done:
                continue
            offered += 1
            if done:
                connected += 1
                when = min((r.created_at or r.updated_at) for r in done if (r.created_at or r.updated_at))
                if first_at and when and when >= first_at:
                    deltas.append((when - first_at).total_seconds() / 60)
            else:
                ignored += 1
        out["rows"].append({"step": label, "offered": offered, "accepted": connected, "declined": "—",
                            "ignored": ignored, "rate": _pct(connected, offered), "median": _minutes(deltas)})

    # water
    w_off = [u for u in users if u.water_offer_status]
    yes = sum(1 for u in w_off if u.water_offer_status == "yes")
    no = sum(1 for u in w_off if u.water_offer_status == "no")
    out["rows"].append({"step": "water reminders", "offered": len(w_off), "accepted": yes, "declined": no,
                        "ignored": len(w_off) - yes - no, "rate": _pct(yes, len(w_off)), "median": "—"})

    # first card
    asked_at = dict(session.query(Message.user_id, func.min(Message.created_at))
                    .filter(Message.user_id.in_(uids), Message.direction == "out",
                            Message.message_type.in_(("card_setup", "workout_intro")))
                    .group_by(Message.user_id).all())
    asked = [u for u in users if u.id in asked_at or u.card_setup_at]
    built = [u for u in asked if u.card_setup_at]
    opened = [u for u in built if getattr(u, "card_opened_at", None)]
    deltas = [((u.card_setup_at - asked_at[u.id]).total_seconds() / 60) for u in built
              if u.id in asked_at and u.card_setup_at > asked_at[u.id]]
    out["rows"].append({"step": "first card (asked → built)", "offered": len(asked), "accepted": len(built), "declined": "—",
                        "ignored": len(asked) - len(built), "rate": _pct(len(built), len(asked)), "median": _minutes(deltas)})
    out["rows"].append({"step": "first card (built → opened)", "offered": len(built), "accepted": len(opened), "declined": "—",
                        "ignored": len(built) - len(opened), "rate": _pct(len(opened), len(built)), "median": "—"})
    return out

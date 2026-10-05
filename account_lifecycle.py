"""Account lifecycle: archive & restart, restore, delete forever (admin-only, 2026-10-05).

Founder: "I want to keep all of my data when I deactivate an account, because I want
to start from zero with a new account to try everything again — but keep the old data
from my old 'nau' account." Plus a Restore button and a SEPARATE, explicit hard delete.

Why a sentinel phone: `users.phone` is UNIQUE NOT NULL and every inbound lookup is
`User.phone == <number>`. A second row for the same number is impossible while the old
row holds it, so archiving must RELEASE the number: `phone` becomes "archived-<id>"
(unique by id, ≤20 chars) and the real E.164 parks in `archived_phone`. The next text
from that phone then matches no user and the ordinary new-user path runs from zero.

What archive touches (ONE transaction on the user row):
  users             archived_at=now, active=False, archived_phone=phone, phone=sentinel,
                    transient pending state cleared (photo meal, active meal, pending
                    calendar event, STOP-confirm, session_state, quiet_until)
  integrations      status -> 'archived' (previous status kept in meta.pre_archive_status;
                    tokens kept — they're for Restore and expire on their own). The sync
                    sweeps + the google_health webhook select base.SYNCABLE_STATUSES only.
  reminders         active -> False (cancelled_at=now)
  workout_sessions  any planned/active session closed with the finalize-if-logged-else-
                    abandon rule (workouts.session_ops.reset_active_session semantics,
                    WITHOUT the summary text / card refresh — nothing is sent)
  held_outbound     'held' rows -> 'expired' (their Message rows flip to 'failed'); the
                    number belongs to whoever signs up next, never deliver old coach texts
  Photon seat       UNTOUCHED — the same phone keeps the line; a new sign-up on the number
                    re-links the same Photon user via photon.add_user's 409 lookup.
Nothing is deleted. Nothing is sent. The all-user sweeps (heartbeat, water, connect
offers, consolidation, episodic, food_logger, adaptive_targets, gym_beats) filter
User.active, so the archived account goes silent.

Restore: refused while ANOTHER row holds `archived_phone` as its phone ("archive or
delete user N first"); otherwise the swap reverses, active=True, and integration rows
that were live become 'revoked' (honest: the tokens are most likely dead; the normal
reconnect path handles it). Reminders stay cancelled.

Delete forever: the old purge (every non-cascading child table) + the User row +
the Photon seat — behind a typed confirmation ("DELETE" or the user id). The seat is
NOT deprovisioned when another row (the restarted account) shares the photon_user_id.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from models import (get_session, User, Message, Meal, Workout, DailyLog, WeightLog, Signal,
                    PantryItem, Place, QueueTicket, WorkoutSession, SetLog, TargetAdjustment,
                    Event, WearableDay, Integration, Reminder, HeldOutbound)

logger = logging.getLogger("cued.account_lifecycle")

ARCHIVED_PHONE_PREFIX = "archived-"
# Typed confirmation accepted by delete-forever (besides the user's own id).
DELETE_CONFIRM_WORD = "DELETE"


class LifecycleError(Exception):
    """A refused lifecycle action: `.message` is admin-facing, `.status` the HTTP code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sentinel_phone(user_id: int) -> str:
    return f"{ARCHIVED_PHONE_PREFIX}{int(user_id)}"


def is_archived(user) -> bool:
    return bool(user is not None and getattr(user, "archived_at", None))


def archived_counts(session, user_id: int) -> dict:
    """Row counts the Archived tab shows — what is being kept."""
    session_ids = [sid for (sid,) in session.query(WorkoutSession.id)
                   .filter(WorkoutSession.user_id == user_id).all()]
    set_logs = (session.query(SetLog.id).filter(SetLog.session_id.in_(session_ids)).count()
                if session_ids else 0)
    return {
        "messages": session.query(Message.id).filter(Message.user_id == user_id).count(),
        "meals": session.query(Meal.id).filter(Meal.user_id == user_id).count(),
        "workout_sessions": len(session_ids),
        "set_logs": set_logs,
        "events": session.query(Event.id).filter(Event.user_id == user_id).count(),
        "wearable_days": session.query(WearableDay.id).filter(WearableDay.user_id == user_id).count(),
    }


# ─── archive & restart ────────────────────────────────────────────────────────

def _close_open_sessions(session, user_id: int, now: datetime) -> tuple[list[int], int]:
    """Finalize-if-logged-else-abandon, inside the caller's transaction. Returns
    (finalized session ids — the caller mirrors them to the legacy table after
    commit, number abandoned)."""
    from workouts.summary import summarize
    finalized, abandoned = [], 0
    open_rows = (session.query(WorkoutSession)
                 .filter(WorkoutSession.user_id == user_id,
                         WorkoutSession.status.in_(("planned", "active"))).all())
    for ws in open_rows:
        logged = (session.query(SetLog.id)
                  .filter(SetLog.session_id == ws.id, SetLog.done.is_(True)).first() is not None)
        if logged:
            ws.status = "done"
            ws.finished_at = now
            s = summarize(session, ws)
            ws.total_volume_lb = s["volume_lb"]
            ws.pr_count = s["pr_count"]
            finalized.append(ws.id)
        else:
            ws.status = "abandoned"
            abandoned += 1
    return finalized, abandoned


def archive_user(user_id: int) -> dict:
    """Archive & restart. Raises LifecycleError (404 unknown, 400 already archived)."""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            raise LifecycleError("User not found.", 404)
        if user.archived_at:
            raise LifecycleError("User is already archived.", 400)
        now = _utcnow()
        real_phone = user.phone
        counts = archived_counts(session, user.id)

        finalized, abandoned = _close_open_sessions(session, user.id, now)

        n_integ = 0
        for row in session.query(Integration).filter(Integration.user_id == user.id).all():
            meta = dict(row.meta or {})
            meta["pre_archive_status"] = row.status
            meta["archived_at"] = now.isoformat()
            row.meta = meta
            row.status = "archived"
            n_integ += 1

        n_rem = (session.query(Reminder)
                 .filter(Reminder.user_id == user.id, Reminder.active.is_(True))
                 .update({Reminder.active: False, Reminder.cancelled_at: now},
                         synchronize_session=False))

        held = (session.query(HeldOutbound)
                .filter(HeldOutbound.user_id == user.id, HeldOutbound.status == "held").all())
        held_message_ids = [mid for h in held for mid in (h.message_ids or [])]
        for h in held:
            h.status = "expired"

        # transient conversation state — a restored account must not resume a
        # half-finished photo meal / calendar confirm / STOP confirm from weeks ago
        user.pending_photo_meal = None
        user.active_meal_id = None
        user.active_meal_updated_at = None
        user.pending_calendar_event = None
        user.pending_optout_confirm = False
        user.session_state = None
        user.quiet_until = None

        user.archived_phone = real_phone
        user.phone = sentinel_phone(user.id)
        user.archived_at = now
        user.active = False
        session.commit()
        name = user.name
    finally:
        session.close()

    # After commit, best-effort derived bookkeeping (never a message, never the card).
    if finalized:
        from workouts.session_ops import mirror_to_legacy
        for sid in finalized:
            try:
                mirror_to_legacy(sid)
            except Exception as e:  # noqa: BLE001
                logger.warning("ACCOUNT_ARCHIVE_MIRROR_FAILED user=%s session=%s err=%s", user_id, sid, e)
    if held_message_ids:
        from sms import _flip_row
        for mid in held_message_ids:
            try:
                _flip_row(mid, provider_sid=None, delivery_status="failed")
            except Exception as e:  # noqa: BLE001
                logger.warning("ACCOUNT_ARCHIVE_HELD_FLIP_FAILED user=%s msg=%s err=%s", user_id, mid, e)

    logger.info("ACCOUNT_ARCHIVED user=%s name=%r msgs=%s meals=%s sessions=%s set_logs=%s events=%s "
                "wearable_days=%s integrations=%s reminders_off=%s held_expired=%s "
                "sessions_finalized=%s sessions_abandoned=%s phone_last4=%s",
                user_id, name, counts["messages"], counts["meals"], counts["workout_sessions"],
                counts["set_logs"], counts["events"], counts["wearable_days"], n_integ, n_rem,
                len(held), len(finalized), abandoned, (real_phone or "")[-4:])
    return {"user_id": user_id, "name": name, "counts": counts,
            "sessions_finalized": len(finalized), "sessions_abandoned": abandoned,
            "integrations_archived": n_integ, "reminders_off": n_rem}


# ─── restore ──────────────────────────────────────────────────────────────────

def phone_holder(session, phone: str, *, exclude_user_id: int | None = None):
    """The user row currently holding `phone` (any status), or None."""
    q = session.query(User).filter(User.phone == phone)
    if exclude_user_id is not None:
        q = q.filter(User.id != exclude_user_id)
    return q.first()


def restore_user(user_id: int) -> dict:
    """Undo archive. Raises LifecycleError (404 unknown, 400 not archived, 409 number held)."""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            raise LifecycleError("User not found.", 404)
        if not user.archived_at or not user.archived_phone:
            raise LifecycleError("User is not archived.", 400)
        holder = phone_holder(session, user.archived_phone, exclude_user_id=user.id)
        if holder:
            raise LifecycleError(
                f"That number is currently held by user {holder.id} ({holder.name}) — "
                f"archive or delete user {holder.id} first.", 409)
        now = _utcnow()
        n_revoked = 0
        for row in session.query(Integration).filter(Integration.user_id == user.id,
                                                     Integration.status == "archived").all():
            meta = dict(row.meta or {})
            prev = meta.pop("pre_archive_status", None)
            meta.pop("archived_at", None)
            if prev in ("pending", "revoked"):
                row.status = prev          # never live → nothing to pretend about
            else:
                # was connected/error: the refresh token has very likely died while
                # archived; say so plainly and let the reconnect path handle it.
                row.status = "revoked"
                meta["revoked_at"] = now.isoformat()
                meta["revoked_reason"] = "restored_from_archive"
                n_revoked += 1
            row.meta = meta
        user.phone = user.archived_phone
        user.archived_phone = None
        user.archived_at = None
        user.active = True
        session.commit()
        name, phone = user.name, user.phone
    finally:
        session.close()
    logger.info("ACCOUNT_RESTORED user=%s name=%r phone_last4=%s integrations_revoked=%s",
                user_id, name, (phone or "")[-4:], n_revoked)
    return {"user_id": user_id, "name": name, "integrations_revoked": n_revoked}


# ─── delete forever ───────────────────────────────────────────────────────────

def purge_user_rows(session, user_id: int) -> None:
    """Delete every child row that does NOT cascade from users (see models.py:
    Message/Meal/Workout/DailyLog/WeightLog/Signal/PantryItem/Place/QueueTicket/
    WorkoutSession(+SetLog)/TargetAdjustment carry plain FKs). The newer tables
    (events, heartbeat_ticks, episodic, token_usage, processed_messages, integrations,
    wearable_days, reminders, held_outbound) cascade or SET NULL on their own. Caller
    deletes the User row and commits."""
    session_ids = [sid for (sid,) in session.query(WorkoutSession.id)
                   .filter(WorkoutSession.user_id == user_id).all()]
    if session_ids:
        session.query(SetLog).filter(SetLog.session_id.in_(session_ids)) \
               .delete(synchronize_session=False)
    for model in (WorkoutSession, QueueTicket, Place, PantryItem, Signal, TargetAdjustment,
                  Message, Meal, WeightLog, Workout, DailyLog):
        session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)


def confirmation_ok(user_id: int, typed: str | None) -> bool:
    typed = (typed or "").strip()
    return bool(typed) and typed in (DELETE_CONFIRM_WORD, str(int(user_id)))


def delete_user_forever(user_id: int, typed_confirmation: str | None) -> dict:
    """Hard delete. Raises LifecycleError (404 unknown, 400 bad confirmation — nothing
    is touched). Works on archived and active users alike."""
    if not confirmation_ok(user_id, typed_confirmation):
        raise LifecycleError(f'Type {DELETE_CONFIRM_WORD} (or the user id {user_id}) to confirm.', 400)
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise LifecycleError("User not found.", 404)
        name = user.name
        photon_user_id = user.photon_user_id
        was_archived = bool(user.archived_at)
        # The restarted account on the same number re-links the SAME Photon user
        # (add_user's 409 lookup) — freeing the seat would cut off the live account.
        seat_shared = bool(photon_user_id) and (
            session.query(User.id).filter(User.photon_user_id == photon_user_id,
                                          User.id != user_id).first() is not None)
        purge_user_rows(session, user_id)
        session.delete(user)
        session.commit()
    finally:
        session.close()
    logger.info("ACCOUNT_DELETED_FOREVER user=%s name=%r was_archived=%s photon_seat=%s",
                user_id, name, was_archived,
                "shared-kept" if seat_shared else ("released" if photon_user_id else "none"))
    deprovisioned = False
    if not seat_shared:
        # Free the shared-pool Photon seat so it doesn't linger (best-effort; a
        # Photon failure never blocks the local delete — the seat can be freed
        # manually). No-op when the user was never provisioned.
        import photon
        deprovisioned = bool(photon.deprovision_user(photon_user_id))
    return {"user_id": user_id, "name": name, "was_archived": was_archived,
            "photon_deprovisioned": deprovisioned, "photon_seat_shared": seat_shared}


__all__ = ["LifecycleError", "archive_user", "restore_user", "delete_user_forever",
           "purge_user_rows", "archived_counts", "is_archived", "sentinel_phone",
           "phone_holder", "confirmation_ok", "ARCHIVED_PHONE_PREFIX", "DELETE_CONFIRM_WORD"]

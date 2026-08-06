"""
Phase 5 — episodic digest. When a conversation goes quiet, a cheap model pass
writes a short dated prose note of the session's non-obvious substance — especially
NON-fitness life context ("orgo midterm tomorrow", "moving apartments this weekend").
This is the heartbeat's raw material for personal follow-ups.

Deliberately distinct from the watermark summarizer (which owns coaching decisions /
fitness progress) — the prompt is scoped OFF that ground so the two don't double-cover.
Stored in its own table (never in user_profile_memory) so nightly consolidation can't
dedupe/close session digests.

Idempotency (it's a non-turn writer): a per-user watermark (last_episodic_message_id)
means the same messages are never digested twice. The sweep only fires when the
conversation has actually gone quiet.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import anthropic
import config
from models import get_session, User, Message, EpisodicDigest, active
from cost_tracking import track

logger = logging.getLogger("cued.episodic")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

DIGEST_PROMPT = """You are keeping a coach's private notebook. Read this quiet-ended conversation and write ONE short dated note (1-2 sentences) capturing the non-obvious, personal substance worth remembering for a future check-in — ESPECIALLY non-fitness life context: exams, deadlines, travel, relationships, mood, big events, work stress.

Do NOT record: fitness/nutrition coaching decisions or progress (that's tracked elsewhere), obvious logistics, or anything already stated as a permanent fact. If nothing personal or non-obvious happened, reply with exactly: NONE

Write only the note (no preamble). Write ABSOLUTE dates, never bare "today"/"tomorrow"/"this afternoon" — this note will be read days later, when those words would resolve to the wrong day. If you write one, it should read like a friend's memory, e.g. "Big orgo midterm Fri morning (Aug 7) — was stressed about it." """


def recent_episodic(user_id: int, days: int = None):
    """Recent episodic notes (soft-delete filtered) for context injection. Newest last."""
    days = days if days is not None else config.EPISODIC_RECENT_DAYS
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    session = get_session()
    try:
        return (active(session, EpisodicDigest, user_id=user_id)
                .filter(EpisodicDigest.occurred_on >= cutoff)
                .order_by(EpisodicDigest.occurred_on).all())
    finally:
        session.close()


def _is_quiet(last_msg) -> bool:
    if not last_msg or not last_msg.created_at:
        return False
    age_min = (datetime.now(timezone.utc)
               - last_msg.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
    return age_min >= config.EPISODIC_QUIET_MINUTES


def digest_user(user_id: int) -> dict:
    """Digest one user's un-digested window IF the conversation has gone quiet and
    there's enough to summarize. Advances the watermark so the same messages are
    never digested twice. Returns a small status dict."""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return {"status": "no_user"}
        watermark = user.last_episodic_message_id or 0
        msgs = (session.query(Message)
                .filter(Message.user_id == user_id, Message.id > watermark)
                .order_by(Message.id).all())
        if len(msgs) < config.EPISODIC_MIN_MESSAGES:
            return {"status": "too_few", "count": len(msgs)}
        if not _is_quiet(msgs[-1]):
            return {"status": "still_active"}  # conversation live — wait, don't advance

        transcript = "\n".join(
            f"{'Coach' if m.direction == 'out' else user.name}: {m.body}" for m in msgs)
        new_watermark = msgs[-1].id
        uid, uname, tz_name = user.id, user.name, user.user_timezone
    finally:
        session.close()

    # Model pass OUTSIDE the lock (network); re-lock to persist + advance watermark.
    note = _run_digest(uid, transcript, tz_name=tz_name)

    session = get_session()
    try:
        user = session.query(User).filter(User.id == uid).with_for_update().first()
        if not user:
            return {"status": "no_user"}
        # Advance the watermark regardless — these messages have been considered.
        user.last_episodic_message_id = new_watermark
        wrote = False
        if note and note.strip().upper() != "NONE":
            # Deterministic de-deixis floor: any relative day-word the writer let
            # through gets its resolved date pinned before the text becomes durable.
            from timefmt import resolve_deixis
            session.add(EpisodicDigest(user_id=uid, text=resolve_deixis(note.strip(), user)))
            wrote = True
        session.commit()
        if wrote:
            logger.info("EPISODIC user=%s note=%r", uid, note.strip()[:80])
        return {"status": "wrote" if wrote else "nothing_notable", "watermark": new_watermark}
    finally:
        session.close()


def _run_digest(user_id: int, transcript: str, *, tz_name: str = None) -> str:
    # Date anchor: without it the writer literally CANNOT resolve "tomorrow", so its
    # only options were bare deixis or silence.
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    local = datetime.now(tz)
    anchor = f"\n\nNow: {local:%A}, {local:%b} {local.day}, {local.year} (user's local time)."
    resp = client.messages.create(
        model=config.EPISODIC_MODEL,
        max_tokens=120,
        system=DIGEST_PROMPT + anchor,
        messages=[{"role": "user", "content": transcript}],
    )
    try:
        track(user_id, "episodic.digest", config.EPISODIC_MODEL, resp)
    except Exception as e:
        logger.warning("EPISODIC_COST_TRACK_FAILED user=%s err=%s", user_id, e)
    return next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")


def digest_all():
    """Sweep: digest every active user whose conversation has gone quiet. Flag-gated."""
    if not config.EPISODIC_ENABLED:
        return
    session = get_session()
    try:
        user_ids = [u.id for u in session.query(User).filter(User.active.is_(True)).all()]
    finally:
        session.close()
    for uid in user_ids:
        try:
            digest_user(uid)
        except Exception as e:
            logger.error("EPISODIC_FAILED user=%s err=%s", uid, e, exc_info=True)

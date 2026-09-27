"""Cross-turn image persistence (the recent-photos store).

The inbound image itself is gone the moment the turn ends — the model never sees it
again. Before this, that meant the coach lost what a photo showed once its turn passed:
it would mislabel it a turn later ("the banana pic"), deny a pic was ever sent, and
re-ask questions the photo had already answered (the 2026-09-26 incident). A structured
meal/fact write (log_meal, remember) survives on its own row; this covers everything
else — the ambiguous photo, the one that only got a conversational reply — by persisting
a compact note of what the coach read off each inbound photo.

Shape: users.recent_photos = [{"at": iso, "caption": str, "summary": str}], newest last,
capped to RECENT_MEDIA_MAX and TTL'd (RECENT_MEDIA_TTL_HOURS) in context. It is a
reference the coach can lean on, never a claim the image is still viewable.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import config


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text[: n - 1] + "…" if len(text) > n else text


def apply_photo(recent: list | None, caption: str, summary: str) -> list:
    """Pure: append a photo record and keep only the RECENT_MEDIA_MAX newest. Returns a
    NEW list. A record with no caption AND no summary is dropped (nothing to reference)."""
    caption = _clip(caption, 160)
    summary = _clip(summary, 240)
    if not caption and not summary:
        return list(recent or [])
    out = list(recent or [])
    out.append({"at": _utcnow().isoformat(), "caption": caption, "summary": summary})
    if len(out) > config.RECENT_MEDIA_MAX:
        out = out[-config.RECENT_MEDIA_MAX :]
    return out


def _is_fresh(at: str | None) -> bool:
    if not at:
        return False
    try:
        ts = datetime.fromisoformat(at)
    except ValueError:
        return False
    return (_utcnow() - ts) <= timedelta(hours=config.RECENT_MEDIA_TTL_HOURS)


def _ago(at: str) -> str:
    try:
        mins = (_utcnow() - datetime.fromisoformat(at)).total_seconds() / 60
    except ValueError:
        return "recently"
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{int(mins)}m ago"
    hrs = mins / 60
    return f"{int(hrs)}h ago" if hrs < 24 else f"{int(hrs / 24)}d ago"


def render_recent_photos_block(recent: list | None) -> str:
    """The context block: only FRESH records (stale ones age out silently). '' when none."""
    if not recent:
        return ""
    fresh = [r for r in recent if isinstance(r, dict) and _is_fresh(r.get("at"))]
    if not fresh:
        return ""
    lines = ["## RECENT PHOTOS THEY SENT (the image itself is gone — this is what you "
             "read off it at the time; reference it, don't re-ask, and never say no pic "
             "was sent)"]
    for r in fresh:
        cap = f'"{r["caption"]}" — ' if r.get("caption") else ""
        summ = r.get("summary") or "(you didn't note what it showed)"
        lines.append(f"- {_ago(r.get('at'))}: {cap}you saw/said: {summ}")
    return "\n".join(lines)


def record_photo(user_id: int, caption: str, summary: str) -> None:
    """Persist a recent-photo record for `user_id`. Row-locks the user and flags the JSON
    column modified so SQLAlchemy actually writes it. Never raises into the caller."""
    if not config.RECENT_MEDIA_ENABLED:
        return
    from sqlalchemy.orm.attributes import flag_modified
    from models import get_session, User

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return
        updated = apply_photo(getattr(user, "recent_photos", None), caption, summary)
        if updated == (user.recent_photos or []):
            return
        user.recent_photos = updated
        flag_modified(user, "recent_photos")
        session.commit()
    finally:
        session.close()

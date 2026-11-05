"""Per-user saved menus (the save_menu tool).

A user sends a menu / meal-plan / list of options as a photo or text "so you can log
me accurately later" — a dining hall, frat-house, or meal-prep menu. Before this, the
coach read it into one reply and it was gone next turn (the "a fact you only read is
NOT saved" gap). save_menu persists it in `users.saved_menus` (JSON), surfaced every
turn via build_loop_context, so "I ate the Wednesday burrito" logs from the saved
macros instead of re-asking or re-guessing.

Shape: {key: {"name": str, "items": [{"item": str, "calories"?: int, "protein_g"?: int,
               "carbs_g"?: int, "fat_g"?: int, "note"?: str}], "captured_at": iso,
               "source_url"?: str, "week_of"?: "YYYY-MM-DD"}}.
Keyed by a normalized name so a re-send REPLACES the same menu. TTL-aged in context
(stale menus stop showing); pruned to the N most-recent on write. A menu that names its
week ("week_of") also stops showing once that week is over — last week's house menu kept
reading as current for a week after it ended (live 2026-11-02, user 77). `source_url` is
where the coach got it (a public menu page / picture) — the seed for finding next week's
by itself.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import config


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())[:60] or "menu"


def _int_or_none(v):
    try:
        return int(round(float(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


def _clean_items(items) -> list[dict]:
    """Keep only entries with a real item name; coerce macros to ints or None."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("item") or it.get("name") or "").strip()
        if not name:
            continue
        row = {"item": name[:80]}
        for k in ("calories", "protein_g", "carbs_g", "fat_g"):
            val = _int_or_none(it.get(k))
            if val is not None:
                row[k] = val
        note = str(it.get("note") or "").strip()
        if note:
            row["note"] = note[:120]
        out.append(row)
        if len(out) >= config.SAVED_MENU_MAX_ITEMS:
            break
    return out


def parse_week_of(value) -> str | None:
    """'YYYY-MM-DD' (or an ISO datetime) → the Monday of that week as 'YYYY-MM-DD'; None
    when unparseable. Menus are dated by the week they cover; the stored date is the
    week's Monday so two readings of the same week collapse."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None
    return (d - timedelta(days=d.weekday())).isoformat()


def apply_menu(saved: dict | None, name: str, items, *, source_url: str | None = None,
               week_of: str | None = None) -> dict:
    """Pure: add/replace the menu keyed by normalized name, then keep only the
    SAVED_MENU_MAX most-recently-captured menus. Returns a NEW dict."""
    saved = dict(saved or {})
    cleaned = _clean_items(items)
    if not cleaned:
        return saved  # nothing usable — don't store an empty menu
    entry = {
        "name": (name or "menu").strip()[:60],
        "items": cleaned,
        "captured_at": _utcnow().isoformat(),
    }
    if source_url:
        entry["source_url"] = str(source_url).strip()[:300]
    wk = parse_week_of(week_of)
    if wk:
        entry["week_of"] = wk
    saved[_key(name)] = entry
    if len(saved) > config.SAVED_MENU_MAX:
        # drop the oldest by captured_at
        ordered = sorted(saved.items(), key=lambda kv: kv[1].get("captured_at") or "", reverse=True)
        saved = dict(ordered[: config.SAVED_MENU_MAX])
    return saved


def _is_fresh(captured_at: str | None, week_of: str | None = None) -> bool:
    if not captured_at:
        return False
    try:
        ts = datetime.fromisoformat(captured_at)
    except ValueError:
        return False
    if (_utcnow() - ts) > timedelta(days=config.SAVED_MENU_TTL_DAYS):
        return False
    wk = parse_week_of(week_of)
    if wk:
        # the week it covers is over: Monday + 7 days (UTC — a day of slack either way
        # beats a menu that reads as current for another week)
        end = datetime.fromisoformat(wk) + timedelta(days=7)
        if _utcnow() >= end:
            return False
    return True


def week_label(week_of: str | None) -> str:
    wk = parse_week_of(week_of)
    if not wk:
        return ""
    try:
        return datetime.fromisoformat(wk).strftime("week of %b %-d")
    except ValueError:
        return ""


def _macro_str(item: dict) -> str:
    bits = []
    if item.get("calories") is not None:
        bits.append(f"{item['calories']} cal")
    if item.get("protein_g") is not None:
        bits.append(f"{item['protein_g']}g protein")
    macros = ", ".join(bits)
    note = item.get("note")
    if macros and note:
        return f"{macros} ({note})"
    return macros or (note or "no macros given")


def fresh_menu_names(saved: dict | None) -> list[str]:
    """Names of the menus still showing in context (fresh, week not over), newest first."""
    if not saved:
        return []
    fresh = [m for m in saved.values()
             if isinstance(m, dict) and _is_fresh(m.get("captured_at"), m.get("week_of"))]
    fresh.sort(key=lambda m: m.get("captured_at") or "", reverse=True)
    return [(m.get("name") or "menu").strip() for m in fresh]


def render_menus_block(saved: dict | None) -> str:
    """The context block: only FRESH menus (stale ones age out silently). '' when none."""
    if not saved:
        return ""
    fresh = [(k, m) for k, m in saved.items()
             if isinstance(m, dict) and _is_fresh(m.get("captured_at"), m.get("week_of"))]
    if not fresh:
        return ""
    fresh.sort(key=lambda kv: kv[1].get("captured_at") or "", reverse=True)
    lines = ["## SAVED MENUS (they sent these so you can log accurately — when they say "
             "they ate one of these, log it from the macros here; don't re-ask or re-guess)"]
    for _k, m in fresh:
        when = ""
        try:
            when = f" (saved {datetime.fromisoformat(m['captured_at']).strftime('%b %-d')})"
        except (ValueError, KeyError):
            pass
        wk = week_label(m.get("week_of"))
        src = " · from their menu page" if m.get("source_url") else ""
        lines.append(f"**{m.get('name') or 'menu'}**{(' (' + wk + ')') if wk else ''}{when}{src}:")
        for it in m.get("items", []):
            lines.append(f"  - {it['item']} — {_macro_str(it)}")
    return "\n".join(lines)


def save_menu_for_user(user_id: int, name: str, items, *, source_url: str | None = None,
                       week_of: str | None = None) -> tuple[int, int]:
    """Persist a menu for `user_id`. Returns (items_saved, total_menus). Row-locks the
    user and flags the JSON column modified so SQLAlchemy actually writes it."""
    from sqlalchemy.orm.attributes import flag_modified
    from models import get_session, User

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return (0, 0)
        updated = apply_menu(user.saved_menus, name, items, source_url=source_url, week_of=week_of)
        saved_count = len((updated.get(_key(name)) or {}).get("items", []))
        user.saved_menus = updated
        flag_modified(user, "saved_menus")
        session.commit()
        return (saved_count, len(updated))
    finally:
        session.close()

"""
Stat cards — small fixed-height bubbles the coach drops into the thread:

  rsf     "rsf right now"  18% full · basically empty. go. · fullness bar
  macros  "today"          calories + protein bars against targets, "low" badge
  week    "this week"      5-day grid: deadlines (calendar radar) + planned lift days

Unlike the workout card (static captions, tap → overlay), these are SHORT and never
scroll, the shape a LIVE bubble handles well: the page renders inline in the thread.
The page is server-rendered here (no JS, no API round trip) from the same readers the
coach already uses: occupancy.now() for the meter, the active meals of the local
nutrition day for totals, schedule.deadline_items() for deadlines, and the split cycle
(workouts.start.infer_template) for lift days. Every render recomputes, so an in-place
update is just the same URL with a new `v`.

No login: a signed, expiring token in the URL names (kind, user). Nothing here invents
a number. A dead meter refuses to send and a missing target shows no bar.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, make_response, render_template_string, request

import config

logger = logging.getLogger("cued.stat_cards")

stat_bp = Blueprint("stat_cards", __name__)

KINDS = ("rsf", "macros", "week")
TOKEN_TTL_S = 7 * 24 * 3600   # a bubble scrolled back to a few days later still renders
_TOKEN_BYTES = 18
WEEK_DAYS = 5
MAX_CHIPS = 3


class StatCardUnavailable(RuntimeError):
    """No honest data for this card right now (meter stale, RSF closed). Don't send."""


# ─── token ───────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    return (config.CARD_TOKEN_SECRET or config.PROFILE_TOKEN_SECRET or config.FLASK_SECRET_KEY).encode("utf-8")


def _mac(kind: str, user_id: int, exp: int) -> str:
    digest = hmac.new(_secret(), f"stat:{kind}:{user_id}:{exp}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def stat_token(kind: str, user_id: int, *, exp: int | None = None, ttl_s: int = TOKEN_TTL_S) -> str:
    exp = int(exp if exp is not None else time.time() + ttl_s)
    return f"{int(user_id)}.{exp}.{_mac(kind, int(user_id), exp)}"


def verify_stat_token(kind: str, token: str | None, *, now: float | None = None) -> int | None:
    """user_id, or None (expired, tampered, wrong kind, malformed)."""
    if kind not in KINDS or not token or token.count(".") != 2:
        return None
    u, e, mac = token.split(".")
    if not (u.isdigit() and e.isdigit()) or len(u) > 12 or len(e) > 12:
        return None
    if int(e) < (now if now is not None else time.time()):
        return None
    if not hmac.compare_digest(mac, _mac(kind, int(u), int(e))):
        return None
    return int(u)


def stat_url(kind: str, user_id: int, *, version: int | None = None) -> str:
    url = f"{config.STAT_CARD_BASE_URL.rstrip('/')}/card/stat/{kind}?t={stat_token(kind, user_id)}"
    return f"{url}&v={version}" if version else url


# ─── state: rsf ──────────────────────────────────────────────────────────────

# The bubble's one line under the number, by occupancy label (occupancy.label_for).
RSF_SUBLINE = {
    "dead": "basically empty. go.",
    "light": "plenty of room.",
    "busy": "filling up.",
    "packed": "packed right now.",
}
RSF_TONE = {"dead": "blue", "light": "blue", "busy": "amber", "packed": "amber", "line": "red"}


def rsf_state(*, now_utc: datetime | None = None) -> dict:
    """The meter as a card. RSF closed or no reading in the last 20 min → a state with
    available=False (the page says so; send_stat_card refuses)."""
    import occupancy
    from integrations.rsf import is_open
    now_utc = now_utc or datetime.now(timezone.utc)
    base = {"kind": "rsf", "label": "rsf right now", "bars": [], "week": []}
    if not is_open(now_utc):
        return {**base, "available": False, "headline": "closed", "subline": "rsf is closed right now.",
                "caption": "rsf · closed", "subcaption": "closed right now"}
    reading = occupancy.now(now_utc.replace(tzinfo=None))
    if not reading:
        return {**base, "available": False, "headline": "no reading", "subline": "the meter's quiet right now.",
                "caption": "rsf · no reading", "subcaption": "the meter's quiet right now"}
    pct, label = reading["pct"], reading["label"]
    if reading["line_on"]:
        wait = reading.get("est_wait_min")
        sub = f"line's on · ~{wait} min." if wait else "line's on."
        tone = "red"
    else:
        sub, tone = RSF_SUBLINE.get(label, ""), RSF_TONE.get(label, "blue")
    return {**base, "available": True, "headline": f"{pct}% full", "subline": sub,
            "bars": [{"label": "", "frac": min(max(pct, 0), 100) / 100, "tone": tone, "badge": None, "value": ""}],
            "foot": f"as of {occupancy.as_of_local(reading)}",
            "caption": f"rsf · {pct}% full", "subcaption": sub}


# ─── state: macros ───────────────────────────────────────────────────────────

# Protein reads "low" when it trails calories by this much of target: 30% of calories
# eaten but 10% of protein is the boba-for-lunch day the badge is for.
PROTEIN_LAG = 0.15


def _today_totals(session, user) -> tuple[int, int]:
    """(calories, protein_g) of today's ACTIVE meals in the user's local nutrition day:
    the same window and rows recompute_daily_totals sums, read without writing."""
    from models import active, Meal
    from timefmt import local_day_bounds
    start, end = local_day_bounds(user)
    meals = active(session, Meal, user_id=user.id).filter(Meal.eaten_at >= start, Meal.eaten_at < end).all()
    return int(sum(m.calories or 0 for m in meals)), int(sum(m.protein_g or 0 for m in meals))


def _frac(n: int, target: int | None) -> float | None:
    return (n / target) if target else None


def macros_state(user_id: int) -> dict:
    from models import get_session, User
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise StatCardUnavailable("no such user")
        cal, pro = _today_totals(session, user)
        cal_t, pro_t = user.calorie_target, user.protein_target
    finally:
        session.close()
    cf, pf = _frac(cal, cal_t), _frac(pro, pro_t)

    cal_badge = ("over", "red") if cf is not None and cf > 1.1 else None
    if pf is not None and pf >= 1:
        pro_badge = ("hit", "green")
    elif pf is not None and cf is not None and cf - pf >= PROTEIN_LAG:
        pro_badge = ("low", "amber")
    else:
        pro_badge = None
    bars = [
        {"label": "calories", "frac": None if cf is None else min(cf, 1.0),
         "tone": "red" if cal_badge else "blue", "badge": cal_badge[0] if cal_badge else None,
         "badge_tone": cal_badge[1] if cal_badge else None,
         "value": f"{cal:,} / {cal_t:,}" if cal_t else f"{cal:,}",
         "big": f"{cal:,}", "of": f"/ {cal_t:,} cal" if cal_t else "cal"},
        {"label": "protein", "frac": None if pf is None else min(pf, 1.0),
         "tone": pro_badge[1] if pro_badge else "blue", "badge": pro_badge[0] if pro_badge else None,
         "badge_tone": pro_badge[1] if pro_badge else None,
         "value": f"{pro} / {pro_t}g" if pro_t else f"{pro}g",
         "big": f"{pro}g", "of": f"/ {pro_t}g" if pro_t else ""},
    ]
    sub = f"{cal:,} cal · {pro}g protein"
    if pro_badge and pro_badge[0] == "low":
        sub += " · protein's low"
    return {"kind": "macros", "label": "today", "available": True, "headline": None, "subline": None,
            "bars": bars, "week": [], "caption": "today", "subcaption": sub,
            "no_targets": not (cal_t or pro_t)}


# ─── state: week ─────────────────────────────────────────────────────────────

_COURSE_TAG_RE = re.compile(r"\[([A-Za-z]{1,10}\s?\d{1,3}[A-Za-z]{0,2})\]")
_COURSE_RE = re.compile(r"\b([A-Za-z]{2,8})\s?(\d{1,3}[A-Za-z]{0,2})\b")
_GENERIC = {"due", "the", "a", "an", "homework", "hw", "assignment", "midterm", "exam", "final", "finals",
            "quiz", "test", "problem", "set", "pset", "project", "paper", "essay", "lab", "reading",
            "submission", "deadline", "turn", "in", "for", "my", "of"}


def chip_label(title: str) -> str:
    """A deadline title → the one word that fits a grid cell. Course tag first
    ('Homework 4: RISC-V [CS61C]' → 'cs61c'), then a course code in the text
    ('CS 70 midterm' → 'cs70'), then the first non-generic word ('Ochem midterm' →
    'ochem'), else the first word ('Midterm 2' → 'midterm')."""
    from schedule import display_title
    t = display_title(title or "")
    m = _COURSE_TAG_RE.search(t) or next((c for c in _COURSE_RE.finditer(_COURSE_TAG_RE.sub("", t))
                                         if c.group(1).lower() not in _GENERIC), None)
    if m:
        return re.sub(r"\s+", "", m.group(0).strip("[]")).lower()
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", t)
    for w in words:
        if w.lower() not in _GENERIC:
            return w.lower()
    return words[0].lower() if words else "due"


def _week_start(today_local):
    """Five days from today; on a weekend, the coming Mon–Fri (Sunday night's 'this week')."""
    wd = today_local.weekday()
    return today_local + timedelta(days=7 - wd) if wd >= 5 else today_local


def _training_weekdays(user) -> set[int]:
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    raw = (getattr(user, "confirmed_training_days", None) or "").lower()
    return {days.index(d.strip()[:3]) for d in raw.split(",") if d.strip()[:3] in days}


def week_state(user_id: int, *, now_utc: datetime | None = None) -> dict:
    from models import get_session, User, WorkoutSession
    from schedule import deadline_items
    from split_pointer import cycle_for
    from timefmt import resolve_tz
    from workouts.start import infer_template
    from workouts.templates import day_label, normalize_template_key

    now_utc = now_utc or datetime.now(timezone.utc)
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise StatCardUnavailable("no such user")
        tz = resolve_tz(user)
        today = now_utc.astimezone(tz).date()
        start = _week_start(today)
        dates = [start + timedelta(days=i) for i in range(WEEK_DAYS)]
        cols = {d: [] for d in dates}

        # Deadlines: the calendar radar, bucketed by LOCAL due date.
        horizon = (dates[-1] - today).days + 1
        for dl in deadline_items(user_id, session, days=horizon, now=now_utc.replace(tzinfo=None)):
            d = dl.when.replace(tzinfo=timezone.utc).astimezone(tz).date()
            if d in cols:
                cols[d].append({"text": chip_label(dl.title), "tone": "exam" if dl.is_exam else "due"})

        # Lift days. Today already trained (the pointer moved today) → that day, done.
        # Then each training day ahead gets the next day in their cycle, starting from
        # infer_template, the same answer the gym beat's "quick legs?" uses.
        trained_today = bool(user.split_pointer_at and user.split_pointer_day and
                             user.split_pointer_at.replace(tzinfo=timezone.utc).astimezone(tz).date() == today)
        planned = {}
        lo = datetime.combine(dates[0], datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)
        hi = datetime.combine(dates[-1] + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc).replace(tzinfo=None)
        for ws in (session.query(WorkoutSession)
                   .filter(WorkoutSession.user_id == user_id, WorkoutSession.date >= lo, WorkoutSession.date < hi,
                           WorkoutSession.status.in_(("planned", "active"))).all()):
            planned[ws.date.replace(tzinfo=timezone.utc).astimezone(tz).date()] = ws.template_key
        weekdays = _training_weekdays(user)
        cycle = [k for k in (normalize_template_key(d) for d in cycle_for(user)) if k]
        nxt = infer_template(user)
        idx = cycle.index(nxt) if nxt in cycle else None
        for d in dates:
            if d == today and trained_today:
                cols[d].append({"text": day_label(user.split_pointer_day), "tone": "done"})
                continue
            if d in planned:
                cols[d].append({"text": day_label(planned[d]), "tone": "lift"})
            elif d.weekday() in weekdays:
                if idx is not None:
                    key = cycle[idx % len(cycle)]
                    idx += 1
                else:
                    key = nxt
                cols[d].append({"text": day_label(key) if key else "lift", "tone": "lift"})
    finally:
        session.close()

    week = []
    for d in dates:
        # Deadlines before lifts, and never more chips than fit; the rest is "+N".
        items = sorted(cols[d], key=lambda c: c["tone"] in ("lift", "done"))
        shown = items if len(items) <= MAX_CHIPS else items[:MAX_CHIPS - 1]
        week.append({"dow": d.strftime("%a").lower(), "date": d.isoformat(), "today": d == today,
                     "items": shown, "more": len(items) - len(shown)})
    n_dl = sum(1 for d in dates for c in cols[d] if c["tone"] in ("exam", "due"))
    n_lift = sum(1 for d in dates for c in cols[d] if c["tone"] == "lift")
    label = "this week" if start.weekday() == 0 else "next 5 days"
    # An all-empty grid says why instead of looking broken.
    empty_note = None
    if not n_dl and not any(cols[d] for d in dates):
        empty_note = "nothing due. tell me your lift days and they'll show here." if not weekdays else "nothing due."
    return {"kind": "week", "label": label, "available": True, "headline": None, "subline": None,
            "bars": [], "week": week, "caption": label, "empty_note": empty_note,
            "subcaption": f"{n_dl} due · {n_lift} lift day{'s' if n_lift != 1 else ''}"}


def build_state(kind: str, user_id: int, *, now_utc: datetime | None = None) -> dict:
    if kind == "rsf":
        return rsf_state(now_utc=now_utc)
    if kind == "macros":
        return macros_state(user_id)
    if kind == "week":
        return week_state(user_id, now_utc=now_utc)
    raise ValueError(f"unknown stat card kind: {kind}")


# ─── page ────────────────────────────────────────────────────────────────────

# The live bubble's frame is set by the Spectrum extension, not the page: on the
# founder's phone (2026-10-05) every card came out ~268×292pt, near square, whatever
# height the page asked for, and a fixed 300px body clipped on the right. So the page
# FILLS the frame: fluid width, full height, content spread top to bottom, overflow
# hidden (never scrolls; the 09-14 live test failed on a page fighting the thread's
# scroll). The launcher icon overlays the top-left ~36px, so it IS the dot before the
# label in the mockups; we leave room for it.

PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{{ s.label }}</title>
<meta property="og:title" content="{{ s.label }}">
{% if og_image %}<meta property="og:image" content="{{ og_image }}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="479">{% endif %}
<style>
  :root { color-scheme: light dark; --fg: #111; --muted: #8a8a8e; --track: #ececef; --cell: #f4f4f6;
          --blue: #2f6bf6; --amber: #f2a93b; --red: #e5484d; --green: #30a46c;
          --amber-bg: #fdf1dc; --amber-fg: #a8650a; --red-bg: #fde4e4; --red-fg: #c2362f;
          --green-bg: #dff3e7; --green-fg: #1f7a4d; }
  @media (prefers-color-scheme: dark) {
    :root { --fg: #f5f5f7; --muted: #98989f; --track: #3a3a3c; --cell: #2c2c2e;
            --amber-bg: #4a3512; --amber-fg: #f5c26b; --red-bg: #4a1e1e; --red-fg: #ff8b85;
            --green-bg: #173826; --green-fg: #6fd39b; }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: transparent; }
  body { display: flex; flex-direction: column; padding: 12px 16px 16px; color: var(--fg);
         font-family: -apple-system, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  .label { flex: none; padding-left: 24px; font-size: 15px; color: var(--muted); line-height: 22px; }
  .main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .track { height: 10px; border-radius: 5px; background: var(--track); overflow: hidden; flex: none; }
  .fill { height: 100%; border-radius: 5px; }
  .fill.blue { background: var(--blue); } .fill.amber { background: var(--amber); }
  .fill.red { background: var(--red); } .fill.green { background: var(--green); }
  .muted { color: var(--muted); }
  .badge { font-size: 13px; font-weight: 600; padding: 2px 9px; border-radius: 10px; white-space: nowrap; }
  .badge.amber { background: var(--amber-bg); color: var(--amber-fg); }
  .badge.red { background: var(--red-bg); color: var(--red-fg); }
  .badge.green { background: var(--green-bg); color: var(--green-fg); }

  /* rsf: the number owns the middle, the bar sits on the floor */
  .rsf .main { justify-content: center; }
  .headline { font-size: clamp(40px, 19vw, 72px); font-weight: 700; letter-spacing: -1px; line-height: 1.05; }
  .subline { font-size: 17px; color: var(--muted); line-height: 22px; margin-top: 4px; }
  .rsf .floor { flex: none; }
  .rsf .foot { font-size: 12px; color: var(--muted); margin-top: 8px; }

  /* macros: two rows spread over the height, number big, target small */
  .macros .main { justify-content: space-evenly; }
  .row-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .row-top .name { font-size: 15px; color: var(--muted); }
  .num { font-size: clamp(24px, 11vw, 40px); font-weight: 700; letter-spacing: -0.5px; line-height: 1.1;
         margin: 2px 0 8px; white-space: nowrap; }
  .num small { font-size: 15px; font-weight: 500; color: var(--muted); letter-spacing: 0; }
  .note { font-size: 13px; color: var(--muted); }

  /* week: the columns take all the height left */
  body.week { padding-left: 12px; padding-right: 12px; }
  .week .label { padding-left: 28px; }
  .week .main { margin-top: 6px; }
  .grid { flex: 1; min-height: 0; display: grid; grid-template-columns: repeat({{ s.week|length or 5 }}, minmax(0, 1fr));
          grid-template-rows: auto 1fr; gap: 4px; }
  .dow { text-align: center; font-size: 13px; color: var(--muted); }
  .dow.today { color: var(--blue); font-weight: 600; }
  .cell { background: var(--cell); border-radius: 8px; min-height: 0; padding: 4px 2px;
          display: flex; flex-direction: column; gap: 4px; overflow: hidden; }
  .chip { font-size: 11px; font-weight: 600; letter-spacing: -0.2px; text-align: center; border-radius: 6px; padding: 3px 0;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: none; }
  .chip.exam { background: var(--red-bg); color: var(--red-fg); }
  .chip.due { background: var(--amber-bg); color: var(--amber-fg); }
  .chip.lift { background: var(--blue); color: #fff; }
  .chip.done { background: var(--green-bg); color: var(--green-fg); }
  .chip.more { color: var(--muted); font-weight: 500; }
  .spacer { flex: 1; }
  .empty { font-size: 13px; color: var(--muted); text-align: center; margin-top: 6px; flex: none; }
</style></head>
<body class="{{ s.kind }}">
<div class="label">{{ s.label }}</div>
{% if s.kind == 'rsf' %}
  <div class="main">
    {% if s.headline %}<div class="headline">{{ s.headline }}</div>{% endif %}
    {% if s.subline %}<div class="subline">{{ s.subline }}</div>{% endif %}
  </div>
  <div class="floor">
    {% for b in s.bars %}<div class="track"><div class="fill {{ b.tone }}" style="width: {{ (b.frac * 100)|round(1) }}%"></div></div>{% endfor %}
    {% if s.foot %}<div class="foot">{{ s.foot }}</div>{% endif %}
  </div>
{% elif s.kind == 'macros' %}
  <div class="main">
  {% for b in s.bars %}
    <div class="row">
      <div class="row-top"><span class="name">{{ b.label }}</span>{% if b.badge %}<span class="badge {{ b.badge_tone }}">{{ b.badge }}</span>{% endif %}</div>
      <div class="num">{{ b.big }}{% if b.of %} <small>{{ b.of }}</small>{% endif %}</div>
      {% if b.frac is not none %}<div class="track"><div class="fill {{ b.tone }}" style="width: {{ (b.frac * 100)|round(1) }}%"></div></div>{% endif %}
    </div>
  {% endfor %}
  {% if s.no_targets %}<div class="note">no targets set yet</div>{% endif %}
  </div>
{% elif s.kind == 'week' %}
  <div class="main">
    <div class="grid">
      {% for d in s.week %}<div class="dow{{ ' today' if d.today else '' }}">{{ d.dow }}</div>{% endfor %}
      {% for d in s.week %}<div class="cell">
        {% for c in d['items'] %}{% if c.tone in ('lift', 'done') %}<div class="spacer"></div>{% endif %}<div class="chip {{ c.tone }}">{{ c.text }}</div>{% endfor %}
        {% if d.more %}<div class="chip more">+{{ d.more }}</div>{% endif %}
      </div>{% endfor %}
    </div>
    {% if s.empty_note %}<div class="empty">{{ s.empty_note }}</div>{% endif %}
  </div>
{% endif %}
</body></html>"""

EXPIRED_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<style>body{margin:0;padding:14px 16px 14px 40px;box-sizing:border-box;font:15px -apple-system,system-ui,sans-serif;color:#8a8a8e;background:transparent}</style>
</head><body>this card expired. text me for a fresh one.</body></html>"""


def render(state: dict, *, og_image: str | None = None) -> str:
    return render_template_string(PAGE_HTML, s=state, og_image=og_image)


@stat_bp.route("/card/stat/<kind>", methods=["GET"])
def stat_card_page(kind):
    """The bubble. Recomputed every fetch: an in-place update is a new `v` on the same URL."""
    user_id = verify_stat_token(kind, request.args.get("t"))
    if user_id is None:
        resp = make_response(EXPIRED_HTML, 401)
    else:
        try:
            state = build_state(kind, user_id)
            # og:image = the same card as a picture: image mode's bubble (the SDK builds a
            # static layout from these tags) and the link preview for web-link users.
            og = (f"{config.STAT_CARD_BASE_URL.rstrip('/')}/card/stat/{kind}/image.png"
                  f"?t={request.args.get('t')}&v={(request.args.get('v') or '')[:12]}")
            resp = make_response(render(state, og_image=og))
            logger.info("STAT_CARD_VIEW user=%s kind=%s v=%s", user_id, kind, (request.args.get("v") or "")[:12])
        except StatCardUnavailable:
            resp = make_response(EXPIRED_HTML, 404)
        # The bubble render is "they're on their phone", same as a workout card open.
        try:
            from wake_model import touch_last_active
            touch_last_active(user_id)
        except Exception:  # noqa: BLE001
            pass
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@stat_bp.route("/card/stat/<kind>/image.png", methods=["GET"])
def stat_card_image(kind):
    """The card as a 2.5:1 PNG (stat_card_image.py), same token as the page."""
    user_id = verify_stat_token(kind, request.args.get("t"))
    if user_id is None:
        return make_response("link expired", 401)
    try:
        from stat_card_image import render_png
        png = render_png(build_state(kind, user_id))
    except StatCardUnavailable:
        return make_response("not found", 404)
    resp = make_response(png)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "no-store"
    logger.info("STAT_CARD_IMAGE user=%s kind=%s bytes=%s", user_id, kind, len(png))
    return resp


# ─── send / update ───────────────────────────────────────────────────────────

def _layout(state: dict) -> dict:
    return {"caption": state["caption"], "subcaption": state["subcaption"], "summary": state["label"]}


MODES = ("live", "image", "static")
BLANK_TITLE = "\u2800"


def _mode(mode: str | None, live: bool | None) -> str:
    """live = the page inline in the bubble (frame fixed ~268×292 by the extension);
    image = a static card whose layout is just the 2.5:1 picture (JPEG via the sidecar's
    imageBase64), about half the height; static = captions only. Default
    STAT_CARDS_MODE; the legacy `live` flag maps to live/static."""
    if mode:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return mode
    if live is not None:
        return "live" if live else "static"
    return config.STAT_CARDS_MODE


def _card_args(mode: str, state: dict) -> dict:
    if mode == "live":
        return {"live": True, "layout": None}
    if mode == "image":
        # The picture IS the bubble: no caption (so no caption strip) and a blank
        # imageTitle (the SDK requires one with an image; letting it build the layout
        # from og tags printed the title over the picture AND in a strip, phone 10-05).
        # U+2800 (braille blank): Photon's upstream strips " " and U+200B and then
        # refuses the image ("image and image_title must be set together"); this one
        # survives and draws nothing.
        import base64
        from stat_card_image import render_jpeg
        return {"live": False, "layout": {"imageBase64": base64.b64encode(render_jpeg(state)).decode("ascii"),
                                          "imageTitle": BLANK_TITLE, "summary": state["label"]}}
    return {"live": False, "layout": _layout(state)}


def _send_with_fallback(phone: str, url: str, mode: str, state: dict, *, user_id: int, kind: str):
    """send_card in `mode`; an image the sidecar/Photon refuses (the U+2800 title is a
    workaround Photon could close) goes again as plain captions, so a Photon-side
    change degrades the card instead of dropping it. → (result, mode actually used)."""
    from photon_cards import send_card, CardError
    try:
        return send_card(phone, url, **_card_args(mode, state)), mode
    except CardError as e:
        if mode != "image":
            raise
        logger.warning("STAT_CARD_IMAGE_REFUSED user=%s kind=%s err=%s — captions instead", user_id, kind, e)
        return send_card(phone, url, **_card_args("static", state)), "static"


def minutes_since_sent(user_id: int, kind: str, *, now_utc: datetime | None = None) -> float | None:
    """Minutes since this card kind last went to the user (any mode), or None."""
    from models import get_session, Message
    session = get_session()
    try:
        row = (session.query(Message.created_at)
               .filter(Message.user_id == user_id, Message.direction == "out",
                       Message.message_type == f"stat_card_{kind}")
               .order_by(Message.created_at.desc()).first())
    finally:
        session.close()
    if not row or not row[0]:
        return None
    now = (now_utc or datetime.now(timezone.utc)).replace(tzinfo=None)
    return (now - row[0]).total_seconds() / 60


def send_stat_card(user_id: int, kind: str, *, mode: str | None = None, live: bool | None = None,
                   now_utc: datetime | None = None) -> dict:
    """Send one stat card to the user's thread in `mode` (live / image / static, see
    _mode; tapping any of them opens the page). A user on the web-link
    fallback gets the URL as text. Raises StatCardUnavailable when there's nothing true
    to show (RSF closed / meter stale) and CardError when the sidecar refuses.

    Returns {provider_message_id, card_session, url, state}. Keep card_session to
    update_stat_card() the bubble in place."""
    from models import get_session, User, Message
    if kind not in KINDS:
        raise ValueError(f"unknown stat card kind: {kind}")
    state = build_state(kind, user_id, now_utc=now_utc)
    if not state.get("available"):
        raise StatCardUnavailable(state.get("subcaption") or "unavailable")
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise StatCardUnavailable("no such user")
        phone, prefers_link = user.phone, bool(getattr(user, "prefers_card_link", False))
    finally:
        session.close()
    url = stat_url(kind, user_id, version=int(time.time()))
    mode = _mode(mode, live)
    from sms import _resolve_channel
    # A card only renders in iMessage. On SMS (green bubbles) or for a user who chose
    # links, the URL goes as text; its preview is the same picture (og:image).
    if (prefers_link and config.CARD_LINK_FALLBACK_ENABLED) or _resolve_channel(user_id) != "imessage":
        from sms import send_sms
        # Plain ASCII so it stays one GSM-7 segment; the preview carries the numbers.
        sid = send_sms(phone, f"{state['label']}: {url}", user_id=user_id, message_type=f"stat_card_{kind}")
        logger.info("STAT_CARD_LINK_SENT user=%s kind=%s id=%s", user_id, kind, sid)
        return {"provider_message_id": sid, "card_session": None, "url": url, "state": state, "mode": "link"}

    r, mode = _send_with_fallback(phone, url, mode, state, user_id=user_id, kind=kind)
    session = get_session()
    try:
        session.add(Message(user_id=user_id, direction="out",
                            body=f"[{state['label']} card: {state['caption']} · {state['subcaption']}]",
                            message_type=f"stat_card_{kind}", channel="imessage",
                            provider_sid=r.get("provider_message_id"), delivery_status="sent"))
        session.commit()
    finally:
        session.close()
    logger.info("STAT_CARD_SENT user=%s kind=%s mode=%s id=%s sub=%s",
                user_id, kind, mode, r.get("provider_message_id"), state["subcaption"])
    return {**r, "url": url, "state": state, "mode": mode}


def update_stat_card(user_id: int, kind: str, card_session: dict, *, mode: str | None = None,
                     live: bool | None = None) -> dict:
    """Re-render the bubble in place (same page, new `v`): the RSF meter ticking, the
    macros bar after a log. Best-effort; raises CardError on refusal."""
    from models import get_session, User
    from photon_cards import update_card, CardError
    state = build_state(kind, user_id)
    session = get_session()
    try:
        user = session.get(User, user_id)
        phone = user.phone if user else None
    finally:
        session.close()
    if not phone:
        raise StatCardUnavailable("no such user")
    mode = _mode(mode, live)
    url = stat_url(kind, user_id, version=int(time.time()))
    try:
        update_card(phone, card_session, url, **_card_args(mode, state))
    except CardError as e:
        if mode != "image":
            raise
        logger.warning("STAT_CARD_IMAGE_REFUSED user=%s kind=%s op=update err=%s — captions instead", user_id, kind, e)
        mode = "static"
        update_card(phone, card_session, url, **_card_args(mode, state))
    logger.info("STAT_CARD_UPDATED user=%s kind=%s mode=%s sub=%s", user_id, kind, mode, state["subcaption"])
    return {"url": url, "state": state, "mode": mode}

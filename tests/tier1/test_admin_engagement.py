"""Admin → Retention: engaged-day rows beside the legacy "any inbound text" rows
(founder, 2026-10-27). A bare reply to a ping is texted-but-not-engaged; a card-only
day is engaged-but-not-texted; a logged meal is core."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.factories import make_user


_LA = ZoneInfo("America/Los_Angeles")


def _now():
    """7pm Pacific today, as naive UTC. The seed lands rows at fixed offsets from this
    instant, and which LOCAL day each one falls on decided the counts: run before ~3pm PT
    and C's card_open (now-15h) shared a local day with its day-38 set (now-30h50m), so C
    had 2 engaged days, not 3, and depth read 2.0. Pinned, the day split is the one the
    assertions describe at any wall-clock hour."""
    local = datetime.now(_LA).replace(hour=19, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _seed(db):
    """Four users, all created 40 days ago, activated the same week:
      A  logs a meal most days (core), texts unprompted → engaged every way
      B  only ever answers our nudges, within minutes ("ok") → texted, never engaged
      C  never texts; trains off the card (done set_logs + a card_open row) → engaged, not texted
      D  dead: no rows at all
    """
    from models import get_session, Message, Meal, WorkoutSession, SetLog, ActivityDay
    s = get_session()
    try:
        t0 = _now() - timedelta(days=40)
        a = make_user(db, name="A", created_at=t0, activated_at=t0)
        b = make_user(db, name="B", created_at=t0, activated_at=t0)
        c = make_user(db, name="C", created_at=t0, activated_at=t0)
        d = make_user(db, name="D", created_at=t0, activated_at=t0)
        for day in range(0, 40, 2):                       # A: a meal + an unprompted text every other day
            at = t0 + timedelta(days=day, hours=18)
            s.add(Meal(user_id=a.id, description="bowl", calories=600, logged_at=at, eaten_at=at, source="text"))
            s.add(Message(user_id=a.id, direction="in", body="had a bowl", message_type="freeform", created_at=at))
        for day in range(0, 40, 3):                       # B: our nudge, their "ok" five minutes later
            at = t0 + timedelta(days=day, hours=12)
            s.add(Message(user_id=b.id, direction="out", body="u eat yet?", message_type="heartbeat", created_at=at))
            s.add(Message(user_id=b.id, direction="in", body="ok", message_type="freeform", created_at=at + timedelta(minutes=5)))
        s.add(Message(user_id=a.id, direction="out", body="morning", message_type="heartbeat", created_at=_now() - timedelta(days=1)))
        for day in (1, 4, 36, 38):                        # C: sets done off the card; one card_open row
            at = t0 + timedelta(days=day, hours=17)
            ws = WorkoutSession(user_id=c.id, date=at, template_key="push", status="done", started_at=at, finished_at=at + timedelta(hours=1))
            s.add(ws); s.flush()
            s.add(SetLog(session_id=ws.id, exercise="bench_press", exercise_label="Bench", set_index=0,
                         actual_weight=135, actual_reps=5, done=True, done_at=at + timedelta(minutes=10), source="card"))
        co = t0 + timedelta(days=39, hours=9)
        # Keyed by the LOCAL day, as activity_days.record keys it (a UTC date here split the
        # same local day into two rows, so today's card_open never counted as today).
        co_day = co.replace(tzinfo=timezone.utc).astimezone(_LA).strftime("%Y-%m-%d")
        s.add(ActivityDay(user_id=c.id, day=co_day, signal="card_open", first_at=co, last_at=co, count=1))
        s.commit()
        return a.id, b.id, c.id, d.id
    finally:
        s.close()


def _run(db):
    from admin_engagement import engagement
    from models import get_session, User, Message
    s = get_session()
    try:
        users = s.query(User).all()
        msgs = s.query(Message).all()
        return engagement(s, users, msgs, now=_now())
    finally:
        s.close()


def test_windows_split_texted_from_engaged_from_core(db):
    _seed(db)
    e = _run(db)
    w = {r["days"]: r for r in e["windows"]}
    # 30d: A + B texted; A + C engaged; A + C core. D nothing.
    assert (w[30]["eligible"], w[30]["texted"], w[30]["engaged"], w[30]["core"]) == (4, 2, 2, 2)
    assert w[30]["engaged_rate"] == "50%"
    # 7d: A texted + engaged; B's last nudge-reply (day 39 = 1d ago) is texted only; C's sets on
    # days 36 + 38 are core and the card_open on day 39 is pull → engaged either way.
    assert (w[7]["texted"], w[7]["engaged"], w[7]["core"]) == (2, 2, 2)


def test_cohort_row_counts_engaged_weeks_after_activation(db):
    _seed(db)
    e = _run(db)
    assert e["cohort_weeks"] == ["W0", "W1", "W2", "W4"]
    assert len(e["cohorts"]) == 2 and e["cohorts"][-1]["label"] == "all activated"
    row = e["cohorts"][0]
    assert row["users"] == 4
    # W0 (days 0–6): A (meal day 0) + C (set day 1) → 2/4. B's "ok" replies never count.
    assert row["w0"] == "2 / 4 (50%)"
    # W1 (days 7–13): A only (C's next set is day 36) → 1/4.
    assert row["w1"] == "1 / 4 (25%)"
    assert row["w4"] == "1 / 4 (25%)"


def test_depth_pull_and_nudge_rate(db):
    _seed(db)
    e = _run(db)
    # Last 28 days: A engaged 14 days (every other day) → 3.5/wk; C engaged 3 days → 0.75/wk. Median of the two.
    assert e["depth_users"] == 2 and e["depth_days_per_week"] == "2.1"
    # Pull: every A day is an unprompted text (14); C's card_open day (1); C's set days are core-only (2).
    assert (e["pull_days"], e["engaged_days"], e["pull_rate"]) == (15, 17, "88%")
    # Nudges in the last 28 days: B's heartbeats on days 12..39 step 3 that fall inside the window + A's one → all of B's answered.
    assert e["nudges_answered"] == e["nudges"] - 1 and e["nudge_rate"] != "—"
    assert e["presence_since"] is not None


def test_today_counts_by_local_day(db):
    """Metrics-page card: A logs a meal + texts NOW (engaged, core, pull, texted); B answers a
    nudge NOW (texted only); C opens the card NOW (engaged + pull, not texted); D nothing."""
    from models import get_session, Message, Meal
    from activity_days import record
    a, b, c, _ = _seed(db)
    now = _now()
    s = get_session()
    try:
        s.add(Meal(user_id=a, description="eggs", calories=300, logged_at=now, eaten_at=now, source="text"))
        s.add(Message(user_id=a, direction="in", body="eggs", message_type="freeform", created_at=now))
        s.add(Message(user_id=b, direction="out", body="u up?", message_type="heartbeat", created_at=now - timedelta(minutes=3)))
        s.add(Message(user_id=b, direction="in", body="ya", message_type="freeform", created_at=now))
        s.commit()
    finally:
        s.close()
    record(c, "card_open", at=now)           # the real recorder: upserts on the (user, local day, signal) key
    t = _run(db)["today"]
    # Seed rows from "yesterday" can share today's local date depending on the hour, so assert floors.
    assert t["engaged"] >= 2 and t["core"] >= 1 and t["pull"] >= 2
    assert t["texted"] >= 2 and t["texted"] >= t["core"]      # A and B texted; B is never core


def test_metrics_page_has_the_engaged_cards(db, client):
    _seed(db)
    html = client.get("/admin").get_data(as_text=True)
    assert "Engaged Today" in html and "Engaged (7d)" in html and "Engaged (30d)" in html
    assert "Active Today" in html            # legacy card still there


def test_retention_page_renders_the_new_section(db, client):
    _seed(db)
    html = client.get("/admin").get_data(as_text=True)
    assert "Engaged, not just texting" in html
    assert "Texted (legacy)" in html and "Core action" in html
    assert "Weekly cohorts" in html and "all activated" in html
    assert "Day 7 — active in last 7 days" in html        # legacy rows untouched


def test_empty_db_renders(db, client):
    html = client.get("/admin").get_data(as_text=True)
    assert "Engaged, not just texting" in html and "No activated users yet" in html
    from admin_engagement import engagement
    from models import get_session
    s = get_session()
    try:
        e = engagement(s, [], [], now=_now())
    finally:
        s.close()
    assert e["depth_days_per_week"] == "—" and e["pull_rate"] == "—" and e["cohorts"] == []

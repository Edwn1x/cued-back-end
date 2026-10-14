"""Admin → Adoption: offered vs accepted per setup step (founder, 2026-10-13)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.factories import make_user


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(db):
    from models import get_session, Message, Integration, User
    s = get_session()
    try:
        t0 = _now() - timedelta(days=2)
        a = make_user(db, name="A", onboarding_step=3, activated_at=t0, onboarding_completed_at=t0 + timedelta(minutes=20),
                      connect_offers={"gcal": (t0 + timedelta(minutes=21)).isoformat()}, water_offer_status="yes",
                      card_setup_at=t0 + timedelta(hours=3), card_opened_at=t0 + timedelta(hours=4))
        b = make_user(db, name="B", phone="+15550100102", onboarding_step=3, activated_at=t0,
                      onboarding_completed_at=t0 + timedelta(minutes=35),
                      connect_offers={"gcal": (t0 + timedelta(minutes=36)).isoformat(), "bcourses": (t0 + timedelta(hours=1)).isoformat()},
                      water_offer_status="no")
        c = make_user(db, name="C", phone="+15550100103", onboarding_step=2, activated_at=t0)        # never finished
        for u, mins in ((a, 0), (b, 0), (c, 0)):
            s.add(Message(user_id=u.id, direction="out", body="hey, it's cued", message_type="onboarding", created_at=t0))
        s.add(Message(user_id=a.id, direction="out", body="before i build ur first card, what do u bench", message_type="card_setup",
                      created_at=t0 + timedelta(hours=2)))
        s.add(Integration(user_id=a.id, provider="gcal", status="connected", created_at=t0 + timedelta(minutes=51),
                          updated_at=t0 + timedelta(minutes=51)))
        s.add(Integration(user_id=b.id, provider="gcal", status="pending", created_at=t0 + timedelta(minutes=40),
                          updated_at=t0 + timedelta(minutes=40)))
        s.commit()
        return a.id, b.id, c.id
    finally:
        s.close()


def test_adoption_counts_offered_accepted_and_time_to_accept(db):
    from admin_adoption import adoption
    from models import get_session
    _seed(db)
    s = get_session()
    try:
        a = adoption(s, days=30)
    finally:
        s.close()
    assert (a["activated"], a["completed"], a["completion_rate"]) == (3, 2, "67%")
    assert a["onboarding_minutes"] in ("28 min",)                      # median of 20 and 35 → 27.5 → "28 min"
    rows = {r["step"]: r for r in a["rows"]}
    gcal = rows["google calendar"]
    assert (gcal["offered"], gcal["accepted"], gcal["ignored"], gcal["rate"], gcal["median"]) == (2, 1, 1, "50%", "30 min")
    assert (rows["bcourses feed"]["offered"], rows["bcourses feed"]["accepted"]) == (1, 0)
    water = rows["water reminders"]
    assert (water["offered"], water["accepted"], water["declined"], water["ignored"]) == (2, 1, 1, 0)
    card = rows["first card (asked → built)"]
    assert (card["offered"], card["accepted"], card["median"]) == (1, 1, "60 min")
    assert rows["first card (built → opened)"]["accepted"] == 1


def test_adoption_page_renders_and_honours_the_window(db, client):
    _seed(db)
    r = client.get("/admin/adoption")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Adoption" in html and "google calendar" in html and "water reminders" in html and "first card" in html
    r7 = client.get("/admin/adoption?days=1")
    assert r7.status_code == 200 and "Activated (1d)" in r7.get_data(as_text=True)
    assert client.get("/admin/adoption?days=zzz").status_code == 200


def test_adoption_empty_window_is_fine(db):
    from admin_adoption import adoption
    from models import get_session
    s = get_session()
    try:
        a = adoption(s, days=30)
    finally:
        s.close()
    assert a["activated"] == 0 and a["completion_rate"] == "—" and a["rows"] == []

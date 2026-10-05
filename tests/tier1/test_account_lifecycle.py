"""Account lifecycle (account_lifecycle.py, 2026-10-05): archive & restart / restore /
delete forever.

Founder: keep ALL the data of the old account, start a brand-new account from zero on
the same number, a Restore button, and a SEPARATE explicit hard delete.

The load-bearing facts: users.phone is UNIQUE, every inbound lookup is by phone, so
archive RELEASES the number (sentinel "archived-<id>", real number in archived_phone);
nothing is deleted; every all-user sweep filters User.active; the integration sweeps
and the google_health webhook select base.SYNCABLE_STATUSES only, so an archived
account never keeps syncing and a new account on the same healthUserId is the only
match.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

import config
from tests.factories import make_user

PHONE = "+15105559031"
SECRET = "test-internal-secret"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(db, *, phone=PHONE, name="Nau", logged_set=True, health_id="HU-OLD"):
    """A lived-in account: messages, meals, an ACTIVE workout session with set logs,
    events, pantry, a wearable day, two integrations (one google_health with a
    healthUserId), an active reminder, a held outbound, pending turn state."""
    from models import (Message, Meal, WorkoutSession, SetLog, Event, PantryItem, WearableDay,
                        Integration, Reminder, HeldOutbound)
    u = make_user(db, phone=phone, name=name, photon_user_id="ph-old",
                  pending_photo_meal='{"x": 1}', session_state={"status": "at_gym"},
                  pending_calendar_event={"title": "x"}, quiet_until=_now() + timedelta(hours=8))
    db.add_all([
        Message(user_id=u.id, direction="in", body="hey"),
        Message(user_id=u.id, direction="out", body="yo"),
        Meal(user_id=u.id, description="eggs", calories=200, protein_g=14),
        Meal(user_id=u.id, description="rice", calories=300, protein_g=6),
        Event(user_id=u.id, event_type="went_to_gym", raw_text="went to rsf"),
        PantryItem(user_id=u.id, item="chicken", source="text"),
        WearableDay(user_id=u.id, provider="google_health", day="2026-10-04", steps=8000),
        Integration(user_id=u.id, provider="google_health", status="connected", external_id=health_id,
                    access_token="enc", refresh_token="enc", meta={"last_sync_at": "x"}),
        Integration(user_id=u.id, provider="gcal", status="connected", external_id="nau@gmail.com", meta={}),
        Integration(user_id=u.id, provider="bcourses", status="pending", meta={}),
        Reminder(user_id=u.id, text="water", local_time="10:00", fire_at=_now() - timedelta(minutes=1)),
        HeldOutbound(user_id=u.id, phone=phone, body="held coach text", status="held",
                     expires_at=_now() + timedelta(hours=2)),
    ])
    db.commit()
    ws = WorkoutSession(user_id=u.id, template_key="push", status="active", started_at=_now())
    db.add(ws); db.commit()
    db.add_all([
        SetLog(session_id=ws.id, exercise="bench_press", exercise_label="Bench", set_index=0,
               planned_weight=135, planned_reps=5, actual_weight=135, actual_reps=5,
               done=logged_set, done_at=_now() if logged_set else None, source="card"),
        SetLog(session_id=ws.id, exercise="bench_press", exercise_label="Bench", set_index=1,
               planned_weight=135, planned_reps=5, done=False),
    ])
    db.commit()
    db.refresh(u)
    return u, ws.id


def _counts(db, uid):
    from account_lifecycle import archived_counts
    db.expire_all()
    return archived_counts(db, uid)


# ─── archive ──────────────────────────────────────────────────────────────────

def test_archive_releases_the_number_and_keeps_every_row(db, client, sms_capture):
    from models import User, Integration, Reminder, HeldOutbound, PantryItem, WearableDay, Message
    u, ws_id = _seed(db)
    uid = u.id
    before = _counts(db, uid)
    assert before == {"messages": 2, "meals": 2, "workout_sessions": 1, "set_logs": 2,
                      "events": 1, "wearable_days": 1}

    r = client.post(f"/admin/user/{uid}/archive")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["status"] == "ok" and body["counts"] == before

    db.expire_all()
    u = db.get(User, uid)
    assert u.archived_at is not None
    assert u.active is False
    assert u.archived_phone == PHONE
    assert u.phone == f"archived-{uid}" and len(u.phone) <= 20
    # transient turn state cleared
    assert u.pending_photo_meal is None and u.session_state is None
    assert u.pending_calendar_event is None and u.quiet_until is None
    # the Photon seat is untouched (same phone keeps the line)
    assert u.photon_user_id == "ph-old"

    # every row still present under the old id
    assert _counts(db, uid) == before
    assert db.query(PantryItem).filter_by(user_id=uid).count() == 1
    assert db.query(WearableDay).filter_by(user_id=uid).count() == 1
    rows = db.query(Integration).filter_by(user_id=uid).all()
    assert len(rows) == 3 and all(r.status == "archived" for r in rows)
    assert {r.provider: r.meta["pre_archive_status"] for r in rows} == {
        "google_health": "connected", "gcal": "connected", "bcourses": "pending"}
    assert all(r.access_token == "enc" for r in rows if r.provider == "google_health")  # tokens kept for Restore
    rem = db.query(Reminder).filter_by(user_id=uid).one()
    assert rem.active is False and rem.cancelled_at is not None
    held = db.query(HeldOutbound).filter_by(user_id=uid).one()
    assert held.status == "expired"
    # nothing was sent to anyone
    assert sms_capture == []
    assert db.query(Message).filter_by(user_id=uid).count() == 2


def test_archive_closes_the_active_session_finalize_if_logged(db, client):
    from models import WorkoutSession, Workout
    u, ws_id = _seed(db, logged_set=True)
    assert client.post(f"/admin/user/{u.id}/archive").status_code == 200
    db.expire_all()
    ws = db.get(WorkoutSession, ws_id)
    assert ws.status == "done" and ws.finished_at is not None
    assert ws.total_volume_lb == 135 * 5 and ws.pr_count is not None
    assert client.get(f"/admin/user/{u.id}").status_code == 200
    # legacy mirror row (best-effort, after commit)
    assert db.query(Workout).filter_by(user_id=u.id).count() >= 1


def test_archive_abandons_an_empty_session(db, client):
    from models import WorkoutSession
    u, ws_id = _seed(db, logged_set=False, phone="+15105559032")
    r = client.post(f"/admin/user/{u.id}/archive")
    assert r.status_code == 200 and r.get_json()["sessions_abandoned"] == 1
    db.expire_all()
    assert db.get(WorkoutSession, ws_id).status == "abandoned"


def test_archive_twice_is_refused_and_unknown_is_404(db, client):
    u, _ = _seed(db)
    assert client.post(f"/admin/user/{u.id}/archive").status_code == 200
    r = client.post(f"/admin/user/{u.id}/archive")
    assert r.status_code == 400 and "already archived" in r.get_json()["message"]
    assert client.post("/admin/user/999999/archive").status_code == 404


def test_admin_send_refuses_an_archived_account(db, client, sms_capture):
    u, _ = _seed(db)
    client.post(f"/admin/user/{u.id}/archive")
    r = client.post("/admin/send", data={"user_id": str(u.id), "body": "hi"})
    assert r.status_code == 400 and "archived" in r.get_json()["message"]
    assert sms_capture == []


# ─── the number is free: the next inbound is a stranger, sign-up makes a NEW user ──

def test_inbound_from_the_archived_number_matches_no_user_and_waitlist_makes_a_new_one(db, client, monkeypatch):
    from models import User, UnknownInbound
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    u, _ = _seed(db)
    old_id = u.id
    client.post(f"/admin/user/{old_id}/archive")

    # a text from the real number → unknown (lead recorded), the old row untouched
    r = client.post("/internal/inbound", headers={"X-Internal-Secret": SECRET},
                    data=json.dumps({"phone": PHONE, "text": "hey cued", "provider_message_id": "p1"}),
                    content_type="application/json")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": False}
    assert db.query(UnknownInbound).filter_by(handle=PHONE).count() == 1

    # the REAL new-user path: the cued.fit form → a brand-new pending row on that number
    body = dict(name="Nau Again", phone=PHONE, sms_consent=True, age="22", gender="male",
                goal=["muscle_building"], experience="none", equipment="full_gym", source="hero")
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    assert r.status_code == 200 and r.get_json()["status"] == "ok", r.get_data(as_text=True)
    db.expire_all()
    new = db.query(User).filter(User.phone == PHONE).one()
    assert new.id != old_id
    assert (new.onboarding_step or 0) == 0 and new.waitlist_status == "pending"
    assert new.active is True and new.archived_at is None
    old = db.get(User, old_id)
    assert old.archived_at is not None and old.phone == f"archived-{old_id}" and old.archived_phone == PHONE
    assert _counts(db, old_id)["messages"] == 2


# ─── sweeps go silent for the archived account ───────────────────────────────

def test_every_all_user_sweep_skips_the_archived_account(db, client, monkeypatch, sms_capture):
    import heartbeat, water_offer, connect_offers, consolidation, episodic, food_logger
    import adaptive_targets, gym_beats, reminders
    from models import Reminder
    live = make_user(db, phone="+15105559040", name="Live", food_logger_status="coexist")
    u, _ = _seed(db)
    db.query(Reminder).filter_by(user_id=live.id).delete()
    db.add(Reminder(user_id=live.id, text="stretch", local_time="10:00",
                    fire_at=_now() - timedelta(minutes=1)))
    live.food_logger_status = "coexist"
    db.commit()
    assert client.post(f"/admin/user/{u.id}/archive").status_code == 200
    archived_id, live_id = u.id, live.id

    for flag in ("HEARTBEAT_ENABLED", "WATER_OFFER_ENABLED", "CONNECT_OFFER_ENABLED", "GCAL_ENABLED",
                 "CONSOLIDATION_ENABLED", "EPISODIC_ENABLED", "RSF_BEATS_ENABLED", "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])

    seen: dict[str, list] = {k: [] for k in ("heartbeat", "water", "connect", "consolidation",
                                             "episodic", "food_logger", "adaptive", "gym_beats")}
    monkeypatch.setattr(heartbeat, "heartbeat_tick", lambda uid: seen["heartbeat"].append(uid))
    monkeypatch.setattr(water_offer, "eligible", lambda user: seen["water"].append(user.id) and False)
    monkeypatch.setattr(connect_offers, "_eligible", lambda user: seen["connect"].append(user.id) and False)
    monkeypatch.setattr(consolidation, "consolidate_user", lambda uid: seen["consolidation"].append(uid))
    monkeypatch.setattr(episodic, "digest_user", lambda uid: seen["episodic"].append(uid))
    monkeypatch.setattr(food_logger, "graduate", lambda uid: seen["food_logger"].append(uid) or {"status": "none"})
    monkeypatch.setattr(adaptive_targets, "apply_cycle", lambda uid: seen["adaptive"].append(uid) or None)
    # gym_beats.sweep imports guardrail_reason from heartbeat inside the function
    monkeypatch.setattr(heartbeat, "guardrail_reason",
                        lambda user, session, **kw: seen["gym_beats"].append(user.id) or "test-skip")

    heartbeat.heartbeat_all()
    water_offer.sweep()
    connect_offers.sweep()
    consolidation.consolidate_all()
    episodic.digest_all()
    food_logger.graduate_all()
    adaptive_targets.run_all()
    gym_beats.sweep()

    for name, ids in seen.items():
        assert archived_id not in ids, f"{name} swept the archived account: {ids}"
        assert live_id in ids, f"{name} did not reach the live user: {ids}"

    # reminders: the archived account's row was deactivated at archive time; the live
    # user's still fires through the ordinary path.
    before = len(sms_capture)
    reminders.fire_due()
    db.expire_all()
    assert db.query(Reminder).filter_by(user_id=archived_id).one().active is False
    assert all(p not in (PHONE, f"archived-{archived_id}") for p, _ in sms_capture)   # never the archived account
    assert len(sms_capture) > before   # the live reminder went out


# ─── google_health webhook + sync: archived rows never match ─────────────────

def test_webhook_and_sync_ignore_archived_rows_and_match_only_the_new_account(db, client, monkeypatch):
    from integrations import google_health_sync as ghs, base
    from models import Integration
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    assert "archived" not in base.SYNCABLE_STATUSES and set(base.SYNCABLE_STATUSES) == {"connected", "error"}

    u, _ = _seed(db, health_id="HU-SHARED")
    assert ghs.users_for_health_ids(["HU-SHARED"]) == [u.id]
    client.post(f"/admin/user/{u.id}/archive")
    assert ghs.users_for_health_ids(["HU-SHARED"]) == []
    synced = []
    monkeypatch.setattr(ghs, "sync_user", lambda uid, **kw: synced.append(uid))
    assert ghs.handle_notifications({"data": {"healthUserId": "HU-SHARED", "dataType": "steps"}},
                                    run_async=False) == []
    assert ghs.sync_all() == 0 and synced == []

    # the restarted account connects the same watch → the ONLY match
    new = make_user(db, phone=PHONE, name="Nau Again")
    db.add(Integration(user_id=new.id, provider="google_health", status="connected", external_id="HU-SHARED", meta={}))
    db.commit()
    assert ghs.users_for_health_ids(["HU-SHARED"]) == [new.id]
    assert ghs.handle_notifications({"data": {"healthUserId": "HU-SHARED", "dataType": "steps"}},
                                    run_async=False) == [new.id]
    assert ghs.sync_all() == 1 and synced == [new.id, new.id]


def test_gcal_and_lms_sync_all_skip_archived_rows(db, client, monkeypatch):
    from integrations import gcal_sync, bcourses, canvas
    from models import Integration
    for flag in ("GCAL_ENABLED", "BCOURSES_ENABLED", "CANVAS_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    u, _ = _seed(db)
    db.add_all([Integration(user_id=u.id, provider="canvas", status="connected", meta={})])
    db.commit()
    seen = []
    monkeypatch.setattr(gcal_sync, "sync_user", lambda uid, **kw: seen.append(("gcal", uid)))
    monkeypatch.setattr(bcourses, "sync_user", lambda uid, **kw: seen.append(("bcourses", uid)))
    monkeypatch.setattr(canvas, "sync_user", lambda uid, **kw: seen.append(("canvas", uid)))
    client.post(f"/admin/user/{u.id}/archive")
    gcal_sync.sync_all(); bcourses.sync_all(); canvas.sync_all()
    assert seen == []


# ─── restore ──────────────────────────────────────────────────────────────────

def test_restore_swaps_the_number_back_and_marks_live_integrations_revoked(db, client):
    from models import User, Integration, Reminder
    u, _ = _seed(db)
    client.post(f"/admin/user/{u.id}/archive")
    r = client.post(f"/admin/user/{u.id}/restore")
    assert r.status_code == 200 and r.get_json()["integrations_revoked"] == 2
    db.expire_all()
    u = db.get(User, u.id)
    assert u.phone == PHONE and u.archived_phone is None and u.archived_at is None and u.active is True
    rows = {r.provider: r for r in db.query(Integration).filter_by(user_id=u.id).all()}
    assert rows["google_health"].status == "revoked" and rows["gcal"].status == "revoked"
    assert rows["google_health"].meta.get("revoked_reason") == "restored_from_archive"
    assert "revoked_at" in rows["google_health"].meta
    assert rows["bcourses"].status == "pending"            # never live → not pretended revoked
    assert all("pre_archive_status" not in r.meta for r in rows.values())
    # reminders stay cancelled (honest: they were cancelled at archive time)
    assert db.query(Reminder).filter_by(user_id=u.id).one().active is False
    assert _counts(db, u.id)["messages"] == 2


def test_restore_is_refused_while_another_account_holds_the_number(db, client):
    from models import User
    u, _ = _seed(db)
    old_id = u.id
    client.post(f"/admin/user/{old_id}/archive")
    new = make_user(db, phone=PHONE, name="Nau Again")
    r = client.post(f"/admin/user/{old_id}/restore")
    assert r.status_code == 409
    msg = r.get_json()["message"]
    assert f"user {new.id}" in msg and "archive or delete" in msg
    db.expire_all()
    old = db.get(User, old_id)
    assert old.archived_at is not None and old.phone == f"archived-{old_id}"   # untouched
    assert db.get(User, new.id).phone == PHONE
    # archive the new one → restore goes through
    assert client.post(f"/admin/user/{new.id}/archive").status_code == 200
    assert client.post(f"/admin/user/{old_id}/restore").status_code == 200
    db.expire_all()
    assert db.get(User, old_id).phone == PHONE


def test_restore_of_a_non_archived_user_is_400(db, client):
    u = make_user(db, phone="+15105559050")
    r = client.post(f"/admin/user/{u.id}/restore")
    assert r.status_code == 400 and "not archived" in r.get_json()["message"]
    assert client.post("/admin/user/999999/restore").status_code == 404


# ─── delete forever ───────────────────────────────────────────────────────────

def test_delete_forever_requires_a_typed_confirmation_and_purges_everything(db, client, monkeypatch):
    from models import (User, Message, Meal, WorkoutSession, SetLog, Event, PantryItem, WearableDay,
                        Integration, Reminder)
    import photon
    u, ws_id = _seed(db)
    uid = u.id
    freed = []
    monkeypatch.setattr(photon, "deprovision_user", lambda pid: freed.append(pid) or True)

    for bad in ({}, {"confirm": ""}, {"confirm": "delete"}, {"confirm": "yes"}, {"confirm": str(uid + 1)}):
        r = client.post(f"/admin/user/{uid}/delete-forever", json=bad)
        assert r.status_code == 400, bad
        assert "DELETE" in r.get_json()["message"]
    db.expire_all()
    assert db.get(User, uid) is not None and _counts(db, uid)["messages"] == 2   # nothing touched
    assert freed == []

    r = client.post(f"/admin/user/{uid}/delete-forever", json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    db.expire_all()
    assert db.get(User, uid) is None
    for model in (Message, Meal, WorkoutSession, Event, PantryItem, WearableDay, Integration, Reminder):
        assert db.query(model).filter_by(user_id=uid).count() == 0, model.__name__
    assert db.query(SetLog).filter_by(session_id=ws_id).count() == 0
    assert freed == ["ph-old"]


def test_delete_forever_works_on_an_archived_account_and_accepts_the_user_id(db, client, monkeypatch):
    from models import User
    import photon
    u, _ = _seed(db)
    uid = u.id
    client.post(f"/admin/user/{uid}/archive")
    freed = []
    monkeypatch.setattr(photon, "deprovision_user", lambda pid: freed.append(pid) or True)
    r = client.post(f"/admin/user/{uid}/delete-forever", data={"confirm": str(uid)})
    assert r.status_code == 200 and r.get_json()["was_archived"] is True
    db.expire_all()
    assert db.get(User, uid) is None and freed == ["ph-old"]


def test_delete_forever_keeps_the_photon_seat_when_the_restarted_account_shares_it(db, client, monkeypatch):
    """The new account on the same number re-links the SAME Photon user (add_user's 409
    lookup). Deleting the old archived row must not cut the live account off the line."""
    import photon
    u, _ = _seed(db)
    client.post(f"/admin/user/{u.id}/archive")
    make_user(db, phone=PHONE, name="Nau Again", photon_user_id="ph-old")
    freed = []
    monkeypatch.setattr(photon, "deprovision_user", lambda pid: freed.append(pid) or True)
    r = client.post(f"/admin/user/{u.id}/delete-forever", json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.get_json()["photon_seat_shared"] is True
    assert freed == []


def test_the_old_delete_route_is_gone(db, client):
    u = make_user(db, phone="+15105559060")
    assert client.post(f"/admin/user/{u.id}/delete").status_code in (404, 405)


# ─── admin pages ──────────────────────────────────────────────────────────────

def test_admin_dashboard_renders_users_and_archived_sections_with_counts(db, client):
    live = make_user(db, phone="+15105559070", name="Livia")
    u, _ = _seed(db)
    client.post(f"/admin/user/{u.id}/archive")
    html = client.get("/admin").get_data(as_text=True)
    # Users tab: the live user has Archive & restart + Delete forever, no plain Delete
    assert f"archiveUser({live.id}" in html and f"deleteForever({live.id}" in html
    assert f"deleteUser(" not in html
    assert f"archiveUser({u.id}" not in html                 # archived rows leave the Users table
    # Archived tab: name, masked real number, archived date, counts, Restore + Delete forever
    assert 'id="page-archived"' in html and f"restoreUser({u.id}" in html and f"deleteForever({u.id}" in html
    start = html.index('id="archived-table"')
    row = html[start:html.index("</table>", start)]
    assert "Nau" in row and f"···{PHONE[-4:]}" in row and PHONE not in row
    cells = [c.strip() for c in row.split("<td")[1:]]
    nums = [c.split(">", 1)[1].split("<", 1)[0].strip() for c in cells]
    # Messages, Meals, Workout Sessions, Set Logs, Events, Wearable Days
    assert nums[4:10] == ["2", "2", "1", "2", "1", "1"], nums
    assert "<span class=\"badge-count\">1</span>" in html[html.index("Archived"):html.index("Archived") + 200]


def test_user_detail_shows_the_archived_banner(db, client):
    u, _ = _seed(db)
    assert "ARCHIVED" not in client.get(f"/admin/user/{u.id}").get_data(as_text=True)
    client.post(f"/admin/user/{u.id}/archive")
    html = client.get(f"/admin/user/{u.id}").get_data(as_text=True)
    assert 'id="archivedBanner"' in html and "ARCHIVED" in html
    assert f"···{PHONE[-4:]}" in html and f"restoreUser({u.id}" in html and f"deleteForever({u.id}" in html


def test_lifecycle_routes_sit_behind_the_admin_basic_auth_gate(db, client, monkeypatch):
    import base64
    from models import User
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret")
    u, _ = _seed(db)
    for path in ("archive", "restore", "delete-forever"):
        assert client.post(f"/admin/user/{u.id}/{path}", json={"confirm": "DELETE"}).status_code == 401
    db.expire_all()
    assert db.get(User, u.id).archived_at is None
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
    assert client.post(f"/admin/user/{u.id}/archive", headers=hdr).status_code == 200


def test_migration_adds_the_archive_columns_idempotently(db):
    import migrate
    from sqlalchemy import text
    stmts = [m for m in migrate.MIGRATIONS if "archived_at" in m or "archived_phone" in m]
    assert stmts == ["ALTER TABLE users ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP",
                     "ALTER TABLE users ADD COLUMN IF NOT EXISTS archived_phone VARCHAR(20)"]
    with migrate_engine().begin() as conn:
        for s in stmts:
            assert migrate.already_applied(conn, s)      # create_all already made them → skip, never lock
            conn.execute(text(s))                        # and the statement itself is a no-op
    cols = {r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='users'")).all()}
    assert {"archived_at", "archived_phone"} <= cols


def migrate_engine():
    import models
    return models.engine


def test_admin_inline_script_parses_and_no_string_literal_spans_a_line(db, client):
    """Live 2026-10-05 (right after #173 deployed): ADMIN_HTML is a PLAIN triple-quoted Python
    string, so the '\\n\\n' written inside the archiveUser confirm and the deleteForever prompt
    rendered as REAL line breaks inside single-quoted JS strings → SyntaxError in the one inline
    <script> → showPage never defined → every nav tab (Users, Waitlist, …) dead. Guard both
    ways: the two literals must carry the two-char JS escape, no JS line may leave a
    single-quoted string open, and when node is around the whole script must parse."""
    import os, re, shutil, subprocess, tempfile
    html = client.get("/admin").get_data(as_text=True)
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "the admin page has an inline <script>"
    assert "')?\\n\\nEvery row" in html and "undone.\\n\\nType DELETE" in html
    assert "function showPage" in html
    for s in scripts:
        for ln in s.split("\n"):
            core = re.sub(r"\\.", "", ln)                 # drop escapes: \' \\ \n …
            core = re.sub(r'"(?:[^"\\]|\\.)*"', '', core)  # drop double-quoted strings
            core = re.sub(r"`[^`]*`", "", core)            # drop single-line template literals
            core = re.sub(r"//.*$", "", core)              # drop line comments
            assert core.count("'") % 2 == 0, f"a single-quoted JS string spans a line break: {ln[:140]!r}"
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(scripts[0]); path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[-500:]
        finally:
            os.unlink(path)

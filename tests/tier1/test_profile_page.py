"""
The user's own profile page: the HMAC link the coach texts them, and the
read-only JSON behind it (GET /profile/<token>). See profile_page.py.
"""

from datetime import datetime, timedelta, timezone

from tests.factories import make_user


def test_token_roundtrip_and_tamper_resistance():
    from profile_page import profile_token, verify_profile_token
    tok = profile_token(27)
    assert tok.startswith("27.") and len(tok.split(".")[1]) == 24
    assert verify_profile_token(tok) == 27
    # same id, wrong mac / another id's mac / garbage → None, never a different user
    uid, mac = tok.split(".")
    assert verify_profile_token(f"{uid}.{mac[:-1]}x") is None
    assert verify_profile_token(f"28.{mac}") is None
    assert verify_profile_token("27") is None
    assert verify_profile_token("") is None
    assert verify_profile_token(None) is None
    assert verify_profile_token("abc.def") is None


def test_profile_url_carries_token_not_phone(db):
    import config
    from profile_page import profile_url, profile_token
    user = make_user(db, name="Nau", phone="+12094205037")
    url = profile_url(user)
    assert url == f"{config.PROFILE_BASE_URL}?t={profile_token(user.id)}"
    assert "2094205037" not in url and "phone" not in url


def test_profile_endpoint_serves_the_users_own_data(db, client):
    from models import Meal, Workout, WeightLog
    from profile_page import profile_token
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    user = make_user(
        db, name="Nau Ruiz", phone="+12094205037", age=21, gender="male",
        occupation="CS student", year="junior", height_ft=5, height_in=10,
        weight_lbs=172.0, goal="fat_loss,muscle_building", biggest_obstacle="consistency",
        calorie_target=2400, protein_target=170, confirmed_training_days="mon,wed,fri",
        confirmed_training_split="ppl", diet="omnivore", restrictions="lactose",
        cooking_situation="dining_hall", wearable="apple_watch",
        user_profile_memory={
            "constraints": [{"id": "c1", "text": "bad left knee — no deep lunges", "ts": "2026-09-01T00:00:00Z", "uses": 0, "safety": True}],
            "goals": [{"id": "g1", "text": "wants to hit 185 bench by December", "ts": "2026-09-02T00:00:00Z", "uses": 0}],
            "__history__": [{"id": "h1", "text": "used to train at 6am", "ts": "2026-08-01T00:00:00Z", "invalidated_at": "2026-09-01T00:00:00Z"}],
        },
    )
    db.add_all([
        Meal(user_id=user.id, eaten_at=now - timedelta(minutes=30), description="chicken bowl",
             calories=650, protein_g=48, carbs_g=60, fat_g=20, source="text"),
        Meal(user_id=user.id, eaten_at=now - timedelta(days=3), description="old pizza",
             calories=900, protein_g=30, carbs_g=100, fat_g=40, source="photo"),
        Meal(user_id=user.id, eaten_at=now - timedelta(minutes=10), description="deleted snack",
             calories=999, protein_g=1, deleted_at=now),
        Workout(user_id=user.id, date=now - timedelta(days=1), workout_type="push",
                exercises=[{"name": "bench", "sets": 3, "reps": 5, "weight": 175}], completed=True),
        WeightLog(user_id=user.id, weighed_at=now - timedelta(days=2), weight_lbs=172.4),
    ])
    db.commit()

    r = client.get(f"/profile/{profile_token(user.id)}")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"
    body = r.get_json()
    assert body["status"] == "ok"
    p = body["profile"]
    assert p["name"] == "Nau Ruiz" and p["first_name"] == "Nau"
    assert p["about"]["height"] == {"ft": 5, "in": 10} and p["about"]["weight_lbs"] == 172
    assert p["goals"]["goals"] == ["fat_loss", "muscle_building"]
    assert p["targets"] == {"calories": 2400, "protein_g": 170}
    assert p["training"]["days"] == ["mon", "wed", "fri"] and p["training"]["split"] == "ppl"
    assert p["nutrition"]["restrictions"] == "lactose"
    # memory: live entries only, history never leaks
    assert [e["text"] for e in p["memory"]["constraints"]] == ["bad left knee — no deep lunges"]
    assert p["memory"]["constraints"][0]["safety"] is True
    assert "goals" in p["memory"] and "__history__" not in p["memory"]
    assert "used to train at 6am" not in str(p["memory"])
    # today's totals = today's non-deleted meals only
    assert p["today"]["calories"] == 650 and p["today"]["meals_logged"] == 1
    # recent tails, newest first, soft-deleted rows filtered
    assert [m["description"] for m in p["recent"]["meals"]] == ["chicken bowl", "old pizza"]
    assert p["recent"]["workouts"][0]["exercises"][0]["name"] == "bench"
    assert p["recent"]["weights"][0]["weight_lbs"] == 172.4


def test_profile_endpoint_rejects_bad_tokens(db, client):
    from profile_page import profile_token
    user = make_user(db, name="Nau")
    good = profile_token(user.id)
    uid, mac = good.split(".")
    for bad in (f"{uid}.{mac[:-2]}zz", f"{int(uid) + 1}.{mac}", uid, "nope", "999999.xxxxxxxx"):
        r = client.get(f"/profile/{bad}")
        assert r.status_code == 404, bad
        assert r.get_json()["status"] == "error"


def test_kickoff_instruction_uses_the_token_link(db, anthropic_stub, monkeypatch):
    """_complete_onboarding hands the model the profile link — it must be the
    token link, never the phone-keyed one."""
    import onboarding_agent
    from profile_page import profile_token
    user = make_user(db, name="Nau", phone="+12094205037", onboarding_step=2,
                     goal="fat_loss", experience="beginner", equipment="full_gym",
                     age=21, gender="male", height_ft=5, height_in=10, weight_lbs=172.0)
    seen = {}

    def fake_generate(system_prompt, instruction, **kw):
        seen["instruction"] = instruction
        return "locked in."

    monkeypatch.setattr(onboarding_agent, "_generate", fake_generate)
    monkeypatch.setattr(onboarding_agent, "send_sms", lambda *a, **k: None)
    onboarding_agent._complete_onboarding(user, "yes")
    assert f"?t={profile_token(user.id)}" in seen["instruction"]
    assert "?phone=" not in seen["instruction"]


def test_admin_user_page_shows_the_profile_link(db, client):
    from profile_page import profile_url
    user = make_user(db, name="Nau")
    r = client.get(f"/admin/user/{user.id}")
    assert r.status_code == 200
    assert profile_url(user) in r.get_data(as_text=True)


def test_training_progress_series(db):
    """Lift progress: one point per local day (top weight wins), Epley e1rm, name
    normalization, weightless exercises ignored; weekly consistency zero-filled."""
    from types import SimpleNamespace
    from profile_page import build_training_progress
    user = make_user(db, name="Nau")
    now = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)   # Fri 1pm Pacific
    d = lambda days, hour=19: (now - timedelta(days=days)).replace(hour=hour, tzinfo=None)
    W = lambda date, ex: SimpleNamespace(date=date, exercises=ex)
    workouts = [
        W(d(0), [{"name": "Bench Press", "sets": 3, "reps": 5, "weight": 185}, {"name": "pull-ups", "sets": 3, "reps": 8}]),
        W(d(0, 22), [{"name": "bench press", "sets": 1, "reps": 3, "weight": 180}]),      # same local day, lower → ignored
        W(d(3), [{"name": " bench  press ", "sets": 4, "reps": 5, "weight": 175}]),
        W(d(10), [{"name": "bench press", "sets": 3, "reps": 5, "weight": 170}, {"name": "squat", "reps": 5, "weight": 225}]),
        W(d(200), [{"name": "bench press", "sets": 3, "reps": 5, "weight": 135}]),    # outside the window
    ]
    prog = build_training_progress(workouts, user, now=now)
    names = [l["name"] for l in prog["lifts"]]
    assert names[0] == "bench press" and len(prog["lifts"]) == 2      # pull-ups had no weight; case/space folded
    bench = prog["lifts"][0]
    assert [s["top_weight"] for s in bench["sessions"]] == [170, 175, 185]
    assert bench["sessions"][-1]["date"] == "2026-09-11" and bench["sessions"][-1]["reps"] == 5
    assert bench["sessions"][-1]["e1rm"] == round(185 * (1 + 5 / 30), 1)
    assert bench["best"]["top_weight"] == 185
    weeks = prog["weekly"]
    assert len(weeks) == 12 and weeks[-1]["week_start"] == "2026-09-07"
    # this week (Mon Sep 7): Tue + two Fri sessions = 3; last week: the Sep 1 session; the 200-day-old one is outside the window
    assert weeks[-1]["workouts"] == 3 and weeks[-2]["workouts"] == 1 and sum(w["workouts"] for w in weeks) == 4


def test_payload_carries_progress_and_local_dates(db, client):
    from models import Workout
    from profile_page import profile_token
    user = make_user(db, name="Nau")
    db.add(Workout(user_id=user.id, date=datetime.now(timezone.utc).replace(tzinfo=None), workout_type="push",
                   exercises=[{"name": "bench", "sets": 3, "reps": 5, "weight": 175}]))
    db.commit()
    p = client.get(f"/profile/{profile_token(user.id)}").get_json()["profile"]
    assert p["training"]["progress"]["lifts"][0]["name"] == "bench"
    assert len(p["training"]["progress"]["weekly"]) == 12
    assert p["recent"]["workouts"][0]["local_date"] == p["today"]["local_date"]


def test_nutrition_daily_series(db, client):
    """14 local days, oldest first, zero-filled; today's and yesterday's meals land on
    their own local days; soft-deleted meals excluded; each meal carries local_date."""
    from models import Meal
    from profile_page import profile_token
    user = make_user(db, name="Nau")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all([
        Meal(user_id=user.id, eaten_at=now - timedelta(minutes=5), description="a", calories=500, protein_g=40),
        Meal(user_id=user.id, eaten_at=now - timedelta(minutes=10), description="b", calories=300, protein_g=20),
        Meal(user_id=user.id, eaten_at=now - timedelta(days=1), description="c", calories=700, protein_g=50),
        Meal(user_id=user.id, eaten_at=now - timedelta(days=1, minutes=1), description="gone", calories=999, deleted_at=now),
        Meal(user_id=user.id, eaten_at=now - timedelta(days=30), description="old", calories=999),
    ])
    db.commit()
    p = client.get(f"/profile/{profile_token(user.id)}").get_json()["profile"]
    daily = p["nutrition"]["daily"]
    assert len(daily) == 14 and daily[-1]["date"] == p["today"]["local_date"]
    assert daily[-1] == {"date": p["today"]["local_date"], "calories": 800, "protein_g": 60, "carbs_g": 0, "fat_g": 0, "meals": 2}
    assert daily[-2]["calories"] == 700 and daily[-2]["meals"] == 1
    assert sum(d["calories"] for d in daily) == 1500
    assert p["recent"]["meals"][0]["local_date"] == p["today"]["local_date"]

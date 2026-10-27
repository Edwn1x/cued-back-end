"""
Live 2026-10-27, user 77: the 2pm posture break fired as a line ("posture break: 10 chin tucks,
doorway stretch 30s each side, lean back over ur chair x10"). He asked "Can you find me reference
videos on how to do these?" — the coach SEARCHED (WEB_SEARCH_QUERY 'chin tuck exercise video
youtube'), then replied "couldn't pull links rn" and sent search terms. The voice rule said NEVER
paste URLs, "only if the user explicitly asks" — he had — and the demo library had lifts only.

Now: (1) the library has verified posture/mobility clips keyed by how the coach phrases them;
(2) the FIRST fire of a reminder naming library movements carries the clips as a second bubble,
once; (3) voice.md makes an explicit ask for a link/video the job, with no capability excuse.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

import config
from exercise_demos import EXERCISE_DEMO_LINKS, movement_phrases, demo_links_for_reminder, _resolve
from tests.factories import make_user

LIVE_REMINDER = "posture break: chin tucks x10, doorway chest stretch 30s each side, lean back over chair x10"
FIRED_LINE = "posture break: 10 chin tucks, doorway stretch 30s each side, lean back over ur chair x10"

POSTURE = {"chin_tuck": "oO9Zernkbxg", "doorway_pec_stretch": "dlQX1pswPwA", "seated_thoracic_extension": "7BQSJxR1i60",
           "wall_angel": "kcca7_JGsVk", "cat_cow": "WHUevrqeKIg", "kneeling_hip_flexor_stretch": "K5EZ8ztNEcw",
           "dead_hang": "bDSqVe4b9pA"}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    monkeypatch.setattr(config, "REMINDER_DEMO_LINKS_ENABLED", True)


def test_posture_keys_point_at_the_verified_clips():
    for key, vid in POSTURE.items():
        assert EXERCISE_DEMO_LINKS[key] == f"https://youtu.be/{vid}", key


@pytest.mark.parametrize("phrase,key", [
    ("chin tucks", "chin_tuck"), ("seated chin tuck", "chin_tuck"),
    ("doorway stretch", "doorway_pec_stretch"), ("doorway chest stretch", "doorway_pec_stretch"),
    ("pec stretch in the doorway", "doorway_pec_stretch"),
    ("lean back over chair", "seated_thoracic_extension"), ("lean back over ur chair", "seated_thoracic_extension"),
    ("thoracic extension", "seated_thoracic_extension"), ("upper back over chair", "seated_thoracic_extension"),
    ("wall angels", "wall_angel"), ("cat cow", "cat_cow"), ("cat-cow stretch", "cat_cow"), ("cat camel", "cat_cow"),
    ("hip flexor stretch", "kneeling_hip_flexor_stretch"), ("dead hangs", "dead_hang"), ("bar hang", "dead_hang"),
])
def test_coach_phrasing_resolves_to_the_same_movement(phrase, key):
    assert _resolve(phrase) == key


@pytest.mark.parametrize("phrase", ["couch stretch", "wall slides", "neck rolls", "shoulder shrugs", "go run"])
def test_different_movements_still_miss(phrase):
    assert _resolve(phrase) is None


def test_movement_phrases_strip_counts_holds_and_the_lead():
    assert movement_phrases(LIVE_REMINDER) == ["chin tucks", "doorway chest stretch", "lean back over chair"]
    assert movement_phrases(FIRED_LINE) == ["chin tucks", "doorway stretch", "lean back over ur chair"]
    assert movement_phrases("stretch break: cat cow x10 and wall angels x10 then dead hang 30s") == ["cat cow", "wall angels", "dead hang"]
    assert movement_phrases("go run") == ["go run"] and movement_phrases("") == []


def test_alexeys_reminder_yields_the_three_clips_in_order():
    d = demo_links_for_reminder(NS(id=1, seen_exercise_demos=None), FIRED_LINE)
    assert list(d.values()) == [EXERCISE_DEMO_LINKS["chin_tuck"], EXERCISE_DEMO_LINKS["doorway_pec_stretch"],
                                EXERCISE_DEMO_LINKS["seated_thoracic_extension"]]
    assert demo_links_for_reminder(NS(id=1, seen_exercise_demos={"chin_tuck": True}), FIRED_LINE).keys() == {"doorway stretch", "lean back over ur chair"}
    assert demo_links_for_reminder(NS(id=1, seen_exercise_demos=None), "go run") == {}


def _out(db, uid):
    from models import Message
    db.expire_all()
    return [(m.message_type, m.body) for m in db.query(Message).filter_by(user_id=uid, direction="out").order_by(Message.id)]


def test_first_fire_carries_the_clips_once_then_never_again(db, sms_capture, anthropic_stub, monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None:
                        type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    from models import Reminder, User
    from reminders import create_reminder, fire_due
    anthropic_stub.reply_with(lambda kw: "posture break: 10 chin tucks, doorway stretch 30s each side, lean back over ur chair x10")
    u = make_user(db, name="Alexey", seen_exercise_demos=None)
    rec = create_reminder(u.id, LIVE_REMINDER, "14:00", days="mon,tue,wed,thu,fri,sat,sun")
    db.expire_all(); at = db.get(Reminder, rec["id"]).fire_at
    assert fire_due(now=at + timedelta(seconds=30)) == 1
    out = _out(db, u.id)
    assert out[-2][0] == "reminder" and out[-2][1].startswith("posture break:")
    assert out[-1][0] == "reminder" and out[-1][1].startswith("how-to clips, about a minute each:")
    body = out[-1][1]
    assert body.count("youtu.be") == 3 and "chin tucks\nhttps://youtu.be/oO9Zernkbxg" in body
    assert "doorway chest stretch\nhttps://youtu.be/dlQX1pswPwA" in body and "lean back over chair\nhttps://youtu.be/7BQSJxR1i60" in body
    db.expire_all()
    assert db.get(User, u.id).seen_exercise_demos == {"chin_tuck": True, "doorway_pec_stretch": True, "seated_thoracic_extension": True}
    # the 8pm fire: the line again, no clips
    db.expire_all(); nxt = db.get(Reminder, rec["id"]).fire_at
    n_before = len(_out(db, u.id))
    assert fire_due(now=nxt + timedelta(seconds=30)) == 1
    out = _out(db, u.id)
    assert len(out) == n_before + 1 and not out[-1][1].startswith("how-to clips")


def test_a_plain_reminder_and_a_water_ping_get_no_clips(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    anthropic_stub.reply_with(lambda kw: "go get that run in")
    u = make_user(db, name="Alex")
    rec = create_reminder(u.id, "go run", "19:30", days="tue,thu")
    db.expire_all(); at = db.get(Reminder, rec["id"]).fire_at
    assert fire_due(now=at + timedelta(seconds=30)) == 1
    assert not any(b.startswith("how-to clips") for _t, b in _out(db, u.id))


def test_flag_off_sends_the_line_only(db, sms_capture, anthropic_stub, monkeypatch):
    monkeypatch.setattr(config, "REMINDER_DEMO_LINKS_ENABLED", False)
    from models import Reminder
    from reminders import create_reminder, fire_due
    anthropic_stub.reply_with(lambda kw: "posture break time")
    u = make_user(db, name="Alexey")
    rec = create_reminder(u.id, LIVE_REMINDER, "14:00", days="mon,tue,wed,thu,fri,sat,sun")
    db.expire_all(); at = db.get(Reminder, rec["id"]).fire_at
    assert fire_due(now=at + timedelta(seconds=30)) == 1
    assert [b for _t, b in _out(db, u.id)] == ["posture break time"]


def test_voice_makes_an_explicit_ask_the_job_and_bans_the_capability_excuse():
    import re
    src = re.sub(r"\s+", " ", open("prompts/voice.md").read())
    assert "NEVER paste URLs" not in src
    assert "when they ASK for a link, a video, a source or a reference" in src
    assert 'Never say "couldn\'t pull links" when you ran a search and have results' in src
    assert "a link from memory is never fine" in src

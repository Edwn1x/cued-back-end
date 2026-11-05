"""
First text back after a long gap → the reply context carries a re-engagement cue
(founder 2026-11-04). Live u33: 26 days dark under water gifs, 👍 to the ping check-in
within a minute, coach answered with a bare tapback because nothing told the reply turn
it was her first text in 26 days. The cue is code-dated off the messages table and says:
react AND one bubble of concrete offers; the check-in variant adds "the pings stay".
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add(user_id, direction, body, *, at, mtype="freeform"):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction=direction, body=body, message_type=mtype, created_at=at))
        s.commit()
    finally:
        s.close()


def _ctx(user_id):
    from models import get_session, User
    from agent_loop import build_loop_context
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def test_long_gap_emits_cue_with_day_count(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=26, hours=2))
    _add(u.id, "out", "[water-break gif]", at=now - timedelta(days=3), mtype="water_reminder")
    _add(u.id, "in", "👍", at=now - timedelta(seconds=20))
    ctx = _ctx(u.id)
    assert "FIRST TEXT FROM THEM IN 26 DAYS" in ctx, ctx
    assert "react-only rule does NOT apply" in ctx
    assert "OFFER two or three concrete things" in ctx
    assert "log today's meals" in ctx
    # not the check-in variant — the last outbound was a plain water ping
    assert "they stay" not in ctx


def test_checkin_reply_names_the_pings(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "ok", at=now - timedelta(days=20))
    _add(u.id, "out", "been a min. still want the water pings? 👍 and they stay, say stop and they're gone",
         at=now - timedelta(minutes=1), mtype="reminder_checkin")
    _add(u.id, "in", "👍", at=now - timedelta(seconds=10))
    ctx = _ctx(u.id)
    assert "FIRST TEXT FROM THEM IN 20 DAYS" in ctx, ctx
    assert "the pings STAY" in ctx and "cancel_reminder" in ctx


def test_short_gap_no_cue(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=2))
    _add(u.id, "in", "ok", at=now - timedelta(seconds=10))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_first_ever_text_no_cue(db):
    from tests.factories import make_user
    u = make_user(db)
    _add(u.id, "in", "hi", at=_now() - timedelta(seconds=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_stale_latest_inbound_no_cue(db):
    """Only the live turn for that text gets the cue — not a context built hours later."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=30))
    _add(u.id, "in", "👍", at=now - timedelta(hours=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_flag_off_no_cue(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "REENGAGE_REPLY_ENABLED", False)
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=30))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_study_room_offer_follows_campus_flag(db, monkeypatch):
    import config
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=10))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=5))
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    assert "send_study_room_link" in _ctx(u.id)
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    assert "send_study_room_link" not in _ctx(u.id)


def test_voice_prompt_carries_the_exception():
    from pathlib import Path
    voice = Path("prompts/voice.md").read_text()
    assert "FIRST TEXT FROM THEM IN N DAYS" in voice


# ---- burst-safe dating, no repeat on the second turn, real-ask wording ----------

def test_burst_of_two_texts_still_gets_the_cue(db):
    """'👍' then 'hey' seconds apart, nothing from the coach between: both are the return."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=20))
    _add(u.id, "out", "still want the water pings?", at=now - timedelta(minutes=1), mtype="reminder_checkin")
    _add(u.id, "in", "👍", at=now - timedelta(seconds=20))
    _add(u.id, "in", "hey", at=now - timedelta(seconds=10))
    ctx = _ctx(u.id)
    assert "FIRST TEXT FROM THEM IN 20 DAYS" in ctx, ctx
    assert "the pings STAY" in ctx


def test_a_tapback_between_burst_texts_does_not_split_it(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=20))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=40))
    _add(u.id, "out", "[reacted 👍 to their message]", at=now - timedelta(seconds=30), mtype="reaction")
    _add(u.id, "in", "hey", at=now - timedelta(seconds=10))
    assert "FIRST TEXT FROM THEM IN 20 DAYS" in _ctx(u.id)


def test_second_turn_of_the_conversation_gets_no_cue(db):
    """The offers bubble went out; their answer to it is an ordinary turn — no repeat."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=20))
    _add(u.id, "in", "👍", at=now - timedelta(minutes=5))
    _add(u.id, "out", "ok they stay. how u been?", at=now - timedelta(minutes=4))
    _add(u.id, "in", "good, just busy", at=now - timedelta(seconds=10))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_already_answered_return_gets_no_cue(db):
    """A context built after the coach already texted back (a continuation) doesn't re-fire."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=20))
    _add(u.id, "in", "👍", at=now - timedelta(minutes=5))
    _add(u.id, "out", "ok they stay. how u been?", at=now - timedelta(minutes=4))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_cue_puts_a_real_ask_before_the_offers(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=9))
    _add(u.id, "in", "can u log a chicken bowl", at=now - timedelta(seconds=5))
    assert "handle that FIRST" in _ctx(u.id)


# ---- the heartbeat reads the same return ----------------------------------------

def _hb_ctx(user_id):
    import heartbeat
    from models import get_session, User
    s = get_session()
    try:
        return heartbeat._proactive_context(s.get(User, user_id), s)
    finally:
        s.close()


def test_heartbeat_does_not_stack_on_the_coach_reply(db):
    """Live u33: 44 min after her 👍 the heartbeat sent 'u eat yet today'."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=26))
    _add(u.id, "out", "still want the water pings?", at=now - timedelta(minutes=46), mtype="reminder_checkin")
    _add(u.id, "in", "👍", at=now - timedelta(minutes=45))
    _add(u.id, "out", "ok they stay. how u been? any meals to log?", at=now - timedelta(minutes=44))
    ctx = _hb_ctx(u.id)
    assert "THEY JUST CAME BACK — first text in 26 days" in ctx, ctx
    assert "do NOT stack another" in ctx
    assert "THIS is the moment" not in ctx


def test_heartbeat_is_the_backstop_when_only_a_tapback_went_back(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=26))
    _add(u.id, "out", "still want the water pings?", at=now - timedelta(minutes=46), mtype="reminder_checkin")
    _add(u.id, "in", "👍", at=now - timedelta(minutes=45))
    _add(u.id, "out", "[reacted 👍 to their message]", at=now - timedelta(minutes=44), mtype="reaction")
    _add(u.id, "out", "[water-break gif]", at=now - timedelta(minutes=5), mtype="water_reminder")
    ctx = _hb_ctx(u.id)
    assert "THIS is the moment for the one re-engagement bubble" in ctx, ctx
    assert "log today's meals" in ctx
    assert "do NOT stack another" not in ctx


def test_heartbeat_block_expires_and_ignores_ordinary_gaps(db, monkeypatch):
    import config
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=26))
    _add(u.id, "in", "👍", at=now - timedelta(hours=19))
    assert "THEY JUST CAME BACK" not in _hb_ctx(u.id)          # past REENGAGE_HEARTBEAT_HOLD_HOURS
    v = make_user(db)
    _add(v.id, "in", "hey", at=now - timedelta(days=2))
    _add(v.id, "in", "ok", at=now - timedelta(minutes=45))
    assert "THEY JUST CAME BACK" not in _hb_ctx(v.id)          # a 2-day gap is not a return
    monkeypatch.setattr(config, "REENGAGE_REPLY_ENABLED", False)
    w = make_user(db)
    _add(w.id, "in", "hey", at=now - timedelta(days=26))
    _add(w.id, "in", "👍", at=now - timedelta(minutes=45))
    assert "THEY JUST CAME BACK" not in _hb_ctx(w.id)          # flag off


# ---- person-specific offers, each with its own condition (founder: "add that as well") ----

def _offers(user_id, **flags):
    from models import get_session, User
    from stale_skip import return_offers
    s = get_session()
    try:
        return return_offers(s.get(User, user_id), session=s)
    finally:
        s.close()


def _menu(name, *, days_ago=1):
    return {name.lower(): {"name": name, "items": [{"item": "bruschetta chicken", "calories": 680, "protein_g": 52}],
                           "captured_at": (_now() - timedelta(days=days_ago)).isoformat()}}


def _event(user_id, title, when, *, source="gcal", all_day=False):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=title, title=title, occurred_at=when, all_day=all_day)


def _day_label(user_id, when):
    from models import get_session, User
    from timefmt import to_local
    s = get_session()
    try:
        return to_local(when, s.get(User, user_id)).strftime("%A")
    finally:
        s.close()


def test_meal_offer_names_their_saved_menu(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    u = make_user(db, saved_menus=_menu("Grizzly Chefs"))
    assert _offers(u.id) == ["log today's meals off their saved 'Grizzly Chefs' menu — they just say what they "
                             "had (a pic or one line works too)"]
    stale = make_user(db, saved_menus=_menu("Grizzly Chefs", days_ago=40))
    assert "saved" not in _offers(stale.id)[0] and "a pic or one line is enough" in _offers(stale.id)[0]


def test_study_room_offer_names_the_exam_on_their_calendar(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    u = make_user(db)
    when = _now() + timedelta(days=3)
    _event(u.id, "CS 61B Midterm", when)
    study = _offers(u.id)[1]
    assert f"ahead of their CS 61B Midterm on {_day_label(u.id, when)}" in study, study
    assert "send_study_room_link" in study and "you can't book it" in study
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    assert "rundown of what's due around their CS 61B Midterm" in _offers(u.id)[1]


def test_near_assignment_is_named_and_a_far_one_is_not(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    near = make_user(db)
    _event(near.id, "due: HW4", _now() + timedelta(days=2), source="bcourses")
    assert "before their HW4 is due" in _offers(near.id)[1], _offers(near.id)
    far = make_user(db)
    _event(far.id, "due: Lab 9", _now() + timedelta(days=8), source="bcourses")
    assert "Lab 9" not in " ".join(_offers(far.id)) and "midterm or finals stretch" in _offers(far.id)[1]


def test_exam_beats_a_nearer_assignment(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    u = make_user(db)
    _event(u.id, "due: HW4", _now() + timedelta(days=1), source="bcourses")
    _event(u.id, "Chem 3A Midterm", _now() + timedelta(days=6))
    assert "Chem 3A Midterm" in _offers(u.id)[1] and "HW4" not in _offers(u.id)[1]


def test_card_only_with_history_week_look_only_with_room_left(db, monkeypatch):
    import config, stale_skip
    import workouts.session_ops as so
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    monkeypatch.setattr(stale_skip, "_calendar_connected", lambda user: True)
    u = make_user(db)
    monkeypatch.setattr(so, "completed_template_keys", lambda uid: ["push"])
    offers = _offers(u.id)                         # one session ≠ history: no card, week look fits
    assert len(offers) == 3 and "week looks like" in offers[2] and not any("card" in o for o in offers)
    monkeypatch.setattr(so, "completed_template_keys", lambda uid: ["push", "pull"])
    offers = _offers(u.id)                         # card takes the third slot; never more than three
    assert len(offers) == 3 and "short easy card" in offers[2] and not any("week looks like" in o for o in offers)


def test_cue_carries_the_named_offers_into_the_reply_context(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    u = make_user(db, saved_menus=_menu("Grizzly Chefs"))
    now = _now()
    _event(u.id, "CS 61B Midterm", now + timedelta(days=3))
    _add(u.id, "in", "hey", at=now - timedelta(days=12))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=5))
    ctx = _ctx(u.id)
    cue = ctx[ctx.index("## FIRST TEXT FROM THEM"):]
    assert "'Grizzly Chefs' menu" in cue and "CS 61B Midterm" in cue, cue[:900]

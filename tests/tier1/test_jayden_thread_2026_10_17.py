"""
Jayden's first day (user 40, 2026-10-17, 15:32–16:31 PT) — seven things the thread exposed,
each fixed in code here. He is 17, in high school (not Berkeley), 5'4"/200, had wrist
surgery months ago and has never used a barbell.

  1. "anything u don't eat, and anything that hurts?" → he answered the wrist half 86s later
     → the re-ask of the diet half was suppressed as a repeat, and the "got it, …" ack was
     stripped as a question → only a 👍 went out.
  2. Onboarding told him 4150 cal/day (teen IOM normal-weight equation at "very active").
  3. "high school student" passed the student gate → the bCourses offer.
  4. "I'm not a Berkeley student…" was typed 19s BEFORE the water offer, lapsed it.
  5. The defaults note asked a never-lifted beginner "what u actually run on full body day".
  6. "never done any exercises using a bar" → the barbell full-body card anyway.
  7. "Okay thank youuu" → the model wrote [silent] without a tapback → nothing at all.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said, reactions, _flags  # noqa: F401 (fixtures)
from tests.tier1.test_water_offer import offer_on  # noqa: F401
from tests.tier1.test_card_setup import setup_on, sidecar_ok, card_ok, sync_threads, _imsg_user  # noqa: F401
from tests.tier1.test_reactions_and_replies import imessage_on, sidecar, _inbound, _rows  # noqa: F401


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ── 1. the repeat guard and the half-answered ask ────────────────────────────

LIVE_6749 = ("got it, so 11 on off nights, 12 if u train. last thing and i'll leave u alone — anything u "
             "don't eat, and anything that hurts or u gotta work around in the gym?")
LIVE_6750 = ("I did get surgery on my right wrist a few months ago for a broken distal radius. If I train "
             "it too hard it does start to ache a bit but most of the time it's fine")
DROPPED = ("got it, we'll just keep anything wrist-heavy light and back off if it starts aching — no reason "
           "to push it. last thing: anything u don't eat?")


def test_got_it_is_an_acknowledgment_not_a_question_lead():
    import onboarding_agent as oa
    assert oa.strip_questions(DROPPED) == ("got it, we'll just keep anything wrist-heavy light and back off if it "
                                           "starts aching — no reason to push it.")
    assert oa._question_part("gotcha. and food — u cooking or dining hall?") == "and food — u cooking or dining hall?"
    assert oa._Q_LEAD_RE.match("got any number at all?")           # a real "got …?" question still leads
    assert not oa._Q_LEAD_RE.match("got it, sleep sounds rough")
    assert not oa._Q_LEAD_RE.match("got u, same here")


def test_the_unanswered_half_of_a_two_part_ask_is_not_a_repeat():
    import onboarding_agent as oa
    assert oa.question_topics(LIVE_6749) == {"diet", "injuries"}
    assert oa.question_topics(DROPPED) == {"diet"}
    missing = [("diet", "dietary preferences or restrictions")]
    stored = {"injuries": "right wrist — surgery…", "diet": None}
    assert oa.unanswered_half_reask(DROPPED, LIVE_6749, stored, missing) == {"diet"}
    # answered nothing of what was asked → still a repeat (user 49, 2026-10-10)
    assert oa.unanswered_half_reask(DROPPED, LIVE_6749, {"activity_level": "desk"}, missing) == set()
    # the asked field is no longer missing → nothing to finish
    assert oa.unanswered_half_reask(DROPPED, LIVE_6749, stored, []) == set()
    # a single-topic ask has no "other half"
    assert oa.unanswered_half_reask("and anything u don't eat?", "anything u don't eat?", stored, missing) == set()


def test_live_user_40_gets_the_wrist_reaction_and_the_diet_ask(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=4, weight_lbs=200, age=17,
                       occupation="high school student", activity_level="active — averages 10,000 steps a day",
                       avg_steps=10000, workout_days="4", workout_time="22:30", current_split="ppl",
                       cooking_situation="mix", wake_time="06:30", sleep_time="23:00",
                       existing_tools="apple_fitness,apple_watch", experience="none")
    _coach_said(user, LIVE_6749)                      # the two-part ask, 86s ago in the live thread
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: ('{"injuries": "right wrist — surgery for a broken distal radius a few '
                                          'months ago; aches if trained too hard"}' if _is_extract(kw) else DROPPED))
    caplog.set_level(logging.INFO)
    assert oa.handle_onboarding_reply(user, LIVE_6750) is False      # still onboarding: diet is open
    assert _bodies(sms_capture) == [DROPPED.replace("—", "-")], _bodies(sms_capture)   # outbound normalises the dash
    assert reactions == []
    assert "ONBOARDING_HALF_ANSWERED_REASK" in caplog.text and "REPEAT_QUESTION_SUPPRESSED" not in caplog.text
    assert _u(user.id).injuries and _u(user.id).diet is None


def test_a_repeat_that_answered_nothing_of_the_ask_is_still_suppressed(db, anthropic_stub, sms_capture, reactions, caplog):
    """User 49's case must not regress: the text answered none of the previous question."""
    import onboarding_agent as oa
    from tests.tier1.test_onboarding_restructure_1 import LIVE_6209, LIVE_6211
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=141, occupation="student", current_split="ppl")
    _coach_said(user, LIVE_6209)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else LIVE_6211)
    caplog.set_level(logging.INFO)
    oa.handle_onboarding_reply(user, "Like either in my room studying or walking around if I go to class")
    assert _bodies(sms_capture) == ["yeah thats real, cs semester keeps u mostly sitting lol."]
    assert "ONBOARDING_REPEAT_QUESTION_SUPPRESSED" in caplog.text


# ── 3. a high-schooler is not a Berkeley student ──────────────────────────────

@pytest.mark.parametrize("occupation,year,expected", [
    ("high school student", None, False), ("highschool student", None, False), ("hs student", None, False),
    ("student", None, True), ("Haas student, works 8 hours a week", None, True),
    ("applied math + cs student", None, True), ("barista", "junior", True), ("barista", None, False),
    ("sophomore in high school", None, False), ("11th grade", None, False), (None, None, False),
])
def test_is_student_means_college(occupation, year, expected):
    from connect_offers import is_student
    from capabilities import _is_student
    u = NS(occupation=occupation, year=year)
    assert is_student(u) is expected and _is_student(u) is expected


def test_a_high_schooler_is_never_offered_bcourses_or_told_about_it(db, monkeypatch):
    import capabilities
    from connect_offers import first_offer_candidates
    monkeypatch.setattr(config, "BCOURSES_ENABLED", True)
    monkeypatch.setattr(config, "CONNECT_OFFER_ENABLED", True)
    hs = make_user(db, occupation="high school student", biggest_obstacle="knowledge", existing_tools="apple_watch")
    cal = make_user(db, occupation="student", biggest_obstacle="knowledge")
    assert "bcourses" not in [p for _k, p, *_r in first_offer_candidates(db, hs, offers={"gcal": "x"})]
    assert "bcourses" in [p for _k, p, *_r in first_offer_candidates(db, cal, offers={"gcal": "x"})]
    assert "bcourses" not in capabilities.build_short_rundown(hs)
    assert "bcourses" not in capabilities.build_rundown(hs) and "midterms" not in capabilities.build_rundown(hs)
    assert "bcourses" in capabilities.build_short_rundown(cal)


# ── 4. a text typed before the offer is not its answer ───────────────────────

def test_a_text_that_predates_the_water_offer_does_not_lapse_it(db, offer_on, caplog):
    from water_offer import send_offer, handle_reply, YES_REPLY
    from reminders import active_reminders
    u = make_user(db, preferred_channel="imessage", wake_time="07:00", sleep_time="23:00")
    _inbound(db, u, "I'm not a Berkeley student so I don't have bcources", "spc-msg-before")   # 1 min ago
    assert send_offer(u.id, source="setup") is True
    caplog.set_level(logging.INFO)
    assert handle_reply(u.id, "I'm not a Berkeley student so I don't have bcources") is None
    assert _u(u.id).water_offer_status == "offered" and "WATER_OFFER_INBOUND_PREDATES" in caplog.text
    _inbound(db, u, "Yes remind me to drink water please", "spc-msg-after", minutes_ago=0)
    assert handle_reply(u.id, "Yes remind me to drink water please") == YES_REPLY.format(n=3)
    rows = active_reminders(u.id)
    assert _u(u.id).water_offer_status == "yes" and len(rows) == 1 and rows[0].source == "offer"


def test_a_text_after_the_offer_still_lapses_it(db, offer_on):
    from water_offer import send_offer, handle_reply
    u = make_user(db, preferred_channel="imessage")
    send_offer(u.id)
    _inbound(db, u, "what time does rsf close", "spc-msg-x", minutes_ago=0)
    assert handle_reply(u.id, "what time does rsf close") is None
    assert _u(u.id).water_offer_status == "lapsed"


# ── 5. the defaults note for someone with nothing to "actually run" ──────────

def test_defaults_note_does_not_quiz_a_beginner_on_their_routine():
    from workouts.start import defaults_note, has_routine
    assert "tell me what u actually run" in defaults_note("full_body")
    note = defaults_note("full_body", has_routine=False)
    assert "actually run" not in note and "not urs yet" not in note and "rather" in note and "full body" in note
    assert has_routine(NS(current_split="ppl", experience="beginner")) is True
    assert has_routine(NS(current_split="none", experience="none")) is False
    assert has_routine(NS(current_split=None, experience="intermediate")) is False
    assert has_routine(NS(current_split="full_body", experience="none")) is False


# ── 6. "never used a bar" → a machine card, every time ───────────────────────

def test_machine_templates_carry_no_barbell_lift():
    from workouts.templates import MACHINE_TEMPLATES, templates_for, day_template, compose_day, TEMPLATES
    barbell = {"squat", "bench_press", "barbell_row", "overhead_press", "romanian_deadlift", "deadlift", "barbell_curl", "skullcrusher"}
    for key, exs in MACHINE_TEMPLATES.items():
        assert not ({t.slug for t in exs} & barbell), key
        assert all(t.default_weight > 0 for t in exs), key          # every machine has a starting number
        assert len({t.slug for t in exs}) == len(exs)
    assert [t.slug for t in MACHINE_TEMPLATES["full_body"]] == ["leg_press", "chest_press_machine", "seated_cable_row",
                                                                "shoulder_press_machine", "leg_extension"]
    assert [t.slug for t in MACHINE_TEMPLATES["legs"]] == ["leg_press", "leg_extension", "leg_curl", "calf_raise"]
    assert templates_for(NS(equipment="machines", custom_templates=None)) == MACHINE_TEMPLATES
    assert templates_for(NS(equipment="full_gym", custom_templates=None)) == TEMPLATES
    assert not ({t.slug for t in day_template(NS(equipment="machines", custom_templates=None), "chest_biceps")} & barbell)
    assert "squat" in {t.slug for t in compose_day("legs")}


@pytest.mark.parametrize("text,flips", [
    ("Leg press I've done about 150. I've never done any exercises using a bar", True),
    ("I like doing mainly machine exercise as I'm just starting and especially because of my wrist injury I'm a bit scared of the bar", True),
    ("machines only pls", True), ("no barbell stuff", True), ("not comfortable with the bar yet", True),
    ("never tried a protein bar", False), ("i bench 135 and squat 185", False), ("meet at the bar after", False),
    ("no clue", False),
])
def test_equipment_preference_capture(db, text, flips):
    from workouts.calibrate import maybe_capture_equipment_preference, wants_machines_only
    u = make_user(db, equipment="full_gym")
    assert wants_machines_only(text) is flips
    assert maybe_capture_equipment_preference(u.id, text, source="test") == ("machines" if flips else None)
    assert _u(u.id).equipment == ("machines" if flips else "full_gym")


def test_a_bodyweight_user_is_not_flipped_to_machines(db):
    from workouts.calibrate import maybe_capture_equipment_preference
    u = make_user(db, equipment="bodyweight")
    assert maybe_capture_equipment_preference(u.id, "machines only", source="test") is None
    assert _u(u.id).equipment == "bodyweight"


def test_live_user_40_first_card_is_the_machine_day(db, setup_on, sidecar_ok, card_ok, sync_threads):
    from workouts.calibrate import set_pending_card, handle_pending_card_reply
    from workouts.start import defaults_note
    from models import get_session, SetLog, WorkoutSession
    u = _imsg_user(db, onboarding_step=3, experience="none", current_split="none", lift_anchors=None,
                   age=17, weight_lbs=200, height_in=64, injuries="right wrist — surgery a few months ago")
    set_pending_card(u.id, "full_body", setup=True)
    assert handle_pending_card_reply(u.id, "Leg press I've done about 150. I've never done any exercises using a bar") is True
    s = get_session()
    try:
        ws = s.query(WorkoutSession).filter(WorkoutSession.user_id == u.id).one()
        slugs = []
        for r in s.query(SetLog).filter(SetLog.session_id == ws.id).order_by(SetLog.id).all():
            if r.exercise not in slugs:
                slugs.append(r.exercise)
        weights = {r.exercise: r.planned_weight for r in s.query(SetLog).filter(SetLog.session_id == ws.id).all()}
    finally:
        s.close()
    assert slugs == ["leg_press", "chest_press_machine", "seated_cable_row", "shoulder_press_machine", "leg_extension"], slugs
    assert weights["leg_press"] > 0 and weights["leg_extension"] > 0      # live: leg extension went out at 0
    assert _u(u.id).equipment == "machines"
    intro = [b for b in sidecar_ok if b.startswith("here's ur first card")][0]
    assert "leg press" in intro and "squat" not in intro, intro
    assert sidecar_ok[-1] == defaults_note("full_body", has_routine=False)


def test_one_off_leg_extension_has_a_starting_weight():
    from workouts.start import one_off_templates
    rows = one_off_templates([{"name": "leg extension", "sets": 3, "reps": 12}, {"name": "stairmaster", "sets": 1, "reps": 17}])
    assert rows[0].slug == "leg_extension" and rows[0].default_weight == 70.0
    assert rows[1].slug == "stairmaster"


# ── 7. a bare [silent] with nothing sent gets the 👍 the rule meant ───────────

def test_silent_sentinel_without_a_tapback_gets_a_thumbs_up_in_code(db, imessage_on, sidecar, anthropic_stub, sms_capture, caplog):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "Okay thank youuu", "spc-msg-ty")
    anthropic_stub.push("[silent]")                      # no react_to_message, no tool
    caplog.set_level(logging.INFO)
    app.process_buffered_message(user.id, "Okay thank youuu", "freeform")
    routes = [r for r, _ in sidecar]
    assert routes.count("react") == 1 and "send" not in routes, routes
    assert [r.message_type for r in _rows(db, user)] == ["reaction"]
    assert sms_capture == [] and "AGENT_LOOP_SILENT_BACKFILLED" in caplog.text


def test_silent_after_a_tool_already_sent_stays_silent(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """start_workout_session's contract: the intro + card went out, the model replies
    [silent] — no tapback on top of a card."""
    import app
    from agent_loop import _sent_since_latest_inbound
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "starting workout", "spc-msg-sw")
    assert _sent_since_latest_inbound(user.id) is False
    from sms import send_sms
    send_sms(user.phone, "here's ur card", user_id=user.id, message_type="workout_intro")   # the tool's send
    assert _sent_since_latest_inbound(user.id) is True
    sidecar.clear()
    anthropic_stub.push("[silent]")
    app.process_buffered_message(user.id, "starting workout", "freeform")
    assert "react" not in [r for r, _ in sidecar]


def test_silent_for_an_sms_user_stays_silent(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """No tapback on SMS — a silent turn there is the legitimate outcome it always was."""
    import app
    user = make_user(db, preferred_channel="sms", onboarding_step=3)
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="ok thanks", message_type="freeform", channel="sms")); s.commit()
    finally:
        s.close()
    anthropic_stub.push("[silent]")
    app.process_buffered_message(user.id, "ok thanks", "freeform")
    assert sms_capture == [] and "react" not in [r for r, _ in sidecar]


def test_silent_backfill_flag_off_is_the_old_silence(db, imessage_on, sidecar, anthropic_stub, monkeypatch):
    import app
    monkeypatch.setattr(config, "SILENT_SENTINEL_BACKFILL_ENABLED", False)
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "Okay thank youuu", "spc-msg-ty2")
    anthropic_stub.push("[silent]")
    app.process_buffered_message(user.id, "Okay thank youuu", "freeform")
    assert "react" not in [r for r, _ in sidecar]

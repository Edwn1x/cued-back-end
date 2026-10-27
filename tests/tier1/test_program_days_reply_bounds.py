"""
Live 2026-10-27, user 77: the program brief ended "those days work, or wanna swap any?" at 11:21.
He answered other things for 47 minutes (46 messages, model replies in between). At 12:07 he
texted "Ok. Also as a heads up, I will be dropping Eleng 105 it's being processed currently.
Ignore anything to do with that class" — handle_days_reply saw a 48h-pending brief and a text
starting with "Ok", locked the days, armed the 8am reminder, sent "locked in. i'll ping u those
days at 8am", and returned before the model ran. "What does that have to do with me dropping
Eleng 105?"

Now a bare yes/no counts only while the brief is still the newest asking outbound AND the text is
one short clause; a list of days / a swap keeps the 48h window; everything else is the model's
turn, and the PROGRAM block tells it the days aren't locked and how to lock them.
"""

from __future__ import annotations

import pytest

from tests.tier1.test_program_builder import _brief_user, _reminders

ELENG = "Ok. Also as a heads up, I will be dropping Eleng 105 it’s being processed currently. Ignore anything to do with that class"


def _coach(user, text, message_type="freeform"):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body=text, message_type=message_type)); s.commit()
    finally:
        s.close()


def _they(user, text):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body=text, message_type="freeform")); s.commit()
    finally:
        s.close()


def _confirmed(db, uid):
    from models import User
    db.expire_all()
    return bool((db.get(User, uid).program or {}).get("days_confirmed"))


@pytest.mark.parametrize("text,bare", [
    ("yes", True), ("yes that works", True), ("ok sounds good", True), ("Yeah those days work!", True),
    ("nah", True), ("no", True), ("yes thats fine thanks", True),
    (ELENG, False), ("yes, and can u add my OH on fridays", False), ("ok so what now", False),
    ("yes those work but can we do mornings instead", False),
])
def test_bare_yes_no_is_one_short_clause(text, bare):
    from workouts.program import _bare_yes_no
    assert bool(_bare_yes_no().match(text)) is bare, text


def test_alexeys_ok_is_not_the_days_answer(db, sms_capture, driver, anthropic_stub, caplog):
    """Brief → his calorie pushback → model replies (the floor moves) → 47 min later "Ok. Also…":
    a normal turn, the model answers the drop, nothing locked, no reminder."""
    import logging
    from workouts.program import send_program_brief, handle_days_reply
    caplog.set_level(logging.INFO)
    u = _brief_user(db)
    send_program_brief(u.id)
    _they(u, "I don't know how feasible the calorie and protein goal is for me")
    _coach(u, "then 3100 isn't realistic. 2650 and 140g makes more sense")
    _coach(u, "next: connect ur google calendar", message_type="connect_offer")
    assert handle_days_reply(u.id, ELENG) is None
    assert "PROGRAM_DAYS_REPLY_NOT_TAKEN" in caplog.text and "floor=False" in caplog.text
    assert not _confirmed(db, u.id) and _reminders(db, u.id, source="program") == []
    sms_capture.clear()
    replies = driver.send(u, ELENG)
    # the agent loop is off in this harness (safe reply) — what matters is that the turn fell
    # through to it instead of the canned line, and nothing got locked
    assert replies and not replies[0].startswith("locked in."), replies
    assert not _confirmed(db, u.id) and _reminders(db, u.id, source="program") == []


def test_a_yes_while_the_brief_has_the_floor_still_locks(db, sms_capture, driver, anthropic_stub):
    from workouts.program import send_program_brief
    u = _brief_user(db)
    send_program_brief(u.id)
    replies = driver.send(u, "yes that works")
    assert replies and replies[0].startswith("locked in.")
    assert anthropic_stub.calls == [] and _confirmed(db, u.id)
    assert len(_reminders(db, u.id, source="program")) == 1


def test_a_reminder_bubble_does_not_take_the_floor_but_a_coach_reply_does(db, sms_capture):
    from workouts.program import send_program_brief, handle_days_reply
    u = _brief_user(db)
    send_program_brief(u.id)
    _coach(u, "drink water", message_type="water_reminder")
    assert handle_days_reply(u.id, "yes") and _confirmed(db, u.id)
    v = _brief_user(db, phone="+15550008883")
    send_program_brief(v.id)
    _coach(v, "yeah physics does that lol")
    assert handle_days_reply(v.id, "yes") is None and not _confirmed(db, v.id)


def test_a_yes_with_a_question_attached_goes_to_the_model_even_on_the_floor(db, sms_capture):
    from workouts.program import send_program_brief, handle_days_reply
    u = _brief_user(db)
    send_program_brief(u.id)
    assert handle_days_reply(u.id, "yes, and can u add my OH on fridays") is None
    assert handle_days_reply(u.id, "ok so what now") is None
    assert not _confirmed(db, u.id)


def test_a_day_list_or_swap_keeps_the_window_after_the_floor_moved(db, sms_capture):
    from workouts.program import send_program_brief, handle_days_reply
    from models import User
    u = _brief_user(db)
    send_program_brief(u.id)
    _coach(u, "yeah physics does that lol")
    line = handle_days_reply(u.id, "can we do mon wed fri instead")
    assert line and line.startswith("locked: ")
    db.expire_all(); assert list(db.get(User, u.id).program["days"]) == ["mon", "wed", "fri"]


def test_program_block_says_the_days_are_not_locked_and_how_to_lock_them(db):
    from workouts.program import send_program_brief, program_context, handle_days_reply
    from models import User
    u = _brief_user(db)
    send_program_brief(u.id)
    db.expire_all(); row = db.get(User, u.id)
    ctx = program_context(row)
    assert "days NOT locked yet" in ctx and "adjust_program with no changes" in ctx
    handle_days_reply(u.id, "yes")
    db.expire_all(); assert "days NOT locked yet" not in program_context(db.get(User, u.id))


def test_adjust_program_with_no_changes_locks_the_days(db, sms_capture):
    from agent_tools import handle_adjust_program
    from workouts.program import send_program_brief
    u = _brief_user(db)
    send_program_brief(u.id)
    assert handle_adjust_program(u.id, {}).startswith("ok: program is now")
    assert _confirmed(db, u.id) and len(_reminders(db, u.id, source="program")) == 1


def test_flag_off_restores_the_old_reading(db, sms_capture, monkeypatch):
    import config
    monkeypatch.setattr(config, "PROGRAM_DAYS_REPLY_BOUNDS_ENABLED", False)
    from workouts.program import send_program_brief, handle_days_reply
    u = _brief_user(db)
    send_program_brief(u.id)
    _coach(u, "yeah physics does that lol")
    assert handle_days_reply(u.id, ELENG) and _confirmed(db, u.id)

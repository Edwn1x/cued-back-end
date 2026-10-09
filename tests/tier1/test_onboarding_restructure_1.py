"""
Onboarding restructure, PR 1 (founder's 12-change list, 2026-10-09 — the conversation phase).
Evidence: the founder's own run as user 48 (2026-10-05, msgs 5776-5830).

  1. Burst → one reply. A text that lands while the previous turn is still being
     answered (or seconds after it flushed) is a CONTINUATION: the buffer marks it, waits
     for the in-flight turn, and the onboarding path either says nothing (👍) or one
     sentence — never the same question in other words (live: 5785/5786, 5800/5801).
  2. Sleep is asked once. "sleep schedule is cooked" pins an estimate in code, the reply
     states the guess, the field stops blocking (live: three clarifiers, 5800-5803).
  3. The big ask names things in plain words, gym things together, steps apart, and
     never sleep (live 5795: "what days u group together and in what order").
  4. Stated assumptions: a Berkeley number is a Berkeley student; the prompt says so.
  5. Voice: "parked", "dragging", "real" (as a reaction) are banned; price-aware.
"""
from __future__ import annotations

import logging
import threading as _real_threading
import time

import pytest

import config
import message_buffer
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE, _is_extract, _new_signup


def _bodies(sms_capture):
    return [b for _p, b in sms_capture]


def _row(db, uid):
    from models import User
    db.expire_all()
    return db.get(User, uid)


def _coach_said(user, text):
    from sms import send_sms
    send_sms(user.phone, text, user_id=user.id, message_type="onboarding")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_CONTINUATION_FOLD_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_SLEEP_ESTIMATE_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_DUP_QUESTION_THRESHOLD", 0.45)
    monkeypatch.setattr(config, "BUFFER_JOIN_WINDOW_S", 2.0)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", False)
    monkeypatch.setattr(config, "WATER_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    message_buffer._buffers.clear()
    message_buffer._in_flight.clear()
    yield
    message_buffer._buffers.clear()
    message_buffer._in_flight.clear()


@pytest.fixture
def reactions(monkeypatch):
    import sms
    got = []
    monkeypatch.setattr(sms, "react_to_latest_inbound", lambda uid, emoji: got.append((uid, emoji)) or True)
    return got


# ── 1. the buffer: continuation turns ─────────────────────────────────────────

def _timer(phone):
    return message_buffer._buffers[phone]["timer"]


def test_text_landing_while_a_turn_is_in_flight_is_a_continuation_and_waits():
    phone = "+15550009001"
    calls = []

    def cb(user_id, body, message_type, image_url, images=None, continuation=False):
        calls.append({"body": body, "continuation": continuation, "t": time.monotonic()})

    # the previous turn is still being answered
    prior = _real_threading.Event()
    message_buffer._in_flight[phone] = prior
    message_buffer.buffer_message(phone, "I'm either walking somewhere or in my room", 1, "freeform",
                                  process_callback=cb, delay_override=(5, 8))
    assert message_buffer._buffers[phone]["continuation"] is True
    t0 = time.monotonic()
    _real_threading.Timer(0.25, prior.set).start()
    _timer(phone).fire()
    assert calls and calls[0]["continuation"] is True
    assert calls[0]["t"] - t0 >= 0.2, "the continuation waited for the in-flight turn"
    assert phone not in message_buffer._in_flight, "the flush cleared its own in-flight mark"


def test_text_seconds_after_a_flush_is_a_continuation_but_a_photo_is_not():
    phone = "+15550009002"
    calls = []

    def cb(user_id, body, message_type, image_url, images=None, **kw):
        calls.append(dict(kw, body=body))

    message_buffer._last_flush[phone] = time.monotonic()
    message_buffer.buffer_message(phone, "Lwk a combo of both", 1, "freeform", process_callback=cb,
                                  delay_override=(5, 8))
    assert message_buffer._buffers[phone]["continuation"] is True
    _timer(phone).fire()
    assert calls[-1].get("continuation") is True

    message_buffer._last_flush[phone] = time.monotonic()
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
    message_buffer.buffer_message(phone, "", 1, "freeform", image_url=img, images=[img],
                                  process_callback=cb, delay_override=(45, 60))
    assert message_buffer._buffers[phone]["continuation"] is False, "a photo is a real new turn"
    _timer(phone).fire()
    assert "continuation" not in calls[-1]


def test_a_callback_without_the_kwarg_still_gets_called():
    phone = "+15550009003"
    calls = []

    def old_cb(user_id, body, message_type, image_url, images=None):
        calls.append(body)

    message_buffer._last_flush[phone] = time.monotonic()
    message_buffer.buffer_message(phone, "and one more thing", 1, "freeform", process_callback=old_cb,
                                  delay_override=(5, 8))
    _timer(phone).fire()
    assert calls == ["and one more thing"]


# ── 1b. the onboarding path on a continuation ─────────────────────────────────

LIVE_5785 = "real. do u cook in ur room or is it dining hall mostly"
LIVE_5786 = "ha so either moving or fully parked, no in between. when ur in ur room all day you cooking or is it dining hall"


def test_live_pair_is_a_duplicate_question_and_distinct_ones_are_not():
    import onboarding_agent as oa
    assert oa.is_duplicate_question(LIVE_5786, LIVE_5785) is True
    assert oa.is_duplicate_question("lol fair. what split u run", LIVE_5785) is False
    assert oa.is_duplicate_question("lol the room all day is its own sport", LIVE_5785) is False, "asks nothing"
    assert oa.is_duplicate_question("", LIVE_5785) is False
    # the second live pair (5800/5801): both ask which way the schedule is cooked
    assert oa.is_duplicate_question("lol ok but cooked which way — like up til 3 and sleeping til noon, or just random every night",
                                    "cooked how, like 3am and up at 11 or all over the place") is True


def test_continuation_skip_sends_nothing_and_reacts(db, anthropic_stub, sms_capture, reactions):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student")
    _coach_said(user, LIVE_5785)
    sms_capture.clear()
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "[skip]"
    anthropic_stub.reply_with(_handler)

    assert oa.handle_onboarding_reply(user, "I'm either walking somewhere or in my room", continuation=True) is False
    assert sms_capture == [], "nothing goes out"
    assert reactions == [(user.id, "like")]
    assert "RIGHT AFTER their previous text" in seen["instruction"] and LIVE_5785 in seen["instruction"]
    assert "do NOT ask it again" in seen["instruction"]


def test_continuation_that_restates_the_question_is_suppressed(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student")
    _coach_said(user, LIVE_5785)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else LIVE_5786)
    caplog.set_level(logging.INFO)

    oa.handle_onboarding_reply(user, "I'm either walking somewhere or in my room", continuation=True)
    assert sms_capture == []
    assert reactions == [(user.id, "like")]
    assert "ONBOARDING_CONTINUATION_SUPPRESSED" in caplog.text and "reason=dup_question" in caplog.text


def test_continuation_with_something_new_gets_one_line(db, anthropic_stub, sms_capture, reactions):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student")
    _coach_said(user, LIVE_5785)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "lol the room all day is its own sport")

    oa.handle_onboarding_reply(user, "jk i never leave my room", continuation=True)
    assert _bodies(sms_capture) == ["lol the room all day is its own sport"]
    assert reactions == []


def test_continuation_flag_is_ignored_when_the_fold_is_off(db, anthropic_stub, sms_capture, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_CONTINUATION_FOLD_ENABLED", False)
    user = _new_signup(db, onboarding_step=2)
    _coach_said(user, LIVE_5785)
    sms_capture.clear()
    seen = []
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.append(kw["messages"][0]["content"]) or "ok"))
    oa.handle_onboarding_reply(user, "mostly cooking", continuation=True)
    assert _bodies(sms_capture) == ["ok"] and "RIGHT AFTER" not in seen[0]


# ── 2. sleep: once, then a stated estimate ────────────────────────────────────

@pytest.mark.parametrize("prev, msg, want", [
    ("cooked how, like 3am and up at 11 or all over the place", "Yeah like staying up hella late and waking up late too", "late"),
    ("roughly when r u up and when do u crash", "idk, sleep schedule is cooked", "late"),
    ("roughly when r u up and when do u crash", "honestly all over the place", "random"),
    ("roughly when r u up and when do u crash", "early, i'm up before class", "early"),
    ("roughly when r u up and when do u crash", "up at 7, down by 11pm", None),      # a clock time: the extractor's
    ("roughly when r u up and when do u crash", "like 2-5am and up around noon", None),
    ("what split u run", "it's cooked ngl", None),                                    # not about sleep
    (None, "my sleep is random", "random"),                                            # on-topic without the ask
])
def test_classify_sleep_answer(prev, msg, want):
    import onboarding_agent as oa
    assert oa.classify_sleep_answer(msg, prev) == want


def test_vague_sleep_answer_pins_an_estimate_the_reply_states_and_stops_asking(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student",
                       activity_level="active", avg_steps=10000, workout_days="4", workout_time="18:00",
                       current_split="ppl", cooking_situation="cook", diet="omnivore", injuries="none",
                       existing_tools="none")
    assert [f[0] for f in oa._get_missing_fields(user)] == ["wake_sleep"]
    _coach_said(user, "roughly when r u up and when do u crash")
    sms_capture.clear()
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "ima guess up around 11 and down by 2, fix it anytime"
    anthropic_stub.reply_with(_handler)

    # the vague answer closes the conversation (nothing else unknown) — the summary reads as a guess
    done = oa.handle_onboarding_reply(user, "And idk, sleep schedule is cooked")
    row = _row(db, user.id)
    assert (row.wake_time, row.sleep_time, row.sleep_estimated) == ("11:00", "02:00", True)
    assert done is True
    summary = [b for b in _bodies(sms_capture) if b.startswith("ok so")][0]
    assert "up around 11 down around 2 (my guess, fix it anytime)" in summary, summary
    assert len(_bodies(sms_capture)) == 2, "the stated-guess bubble + the summary, nothing else"


def test_vague_sleep_answer_mid_conversation_states_the_guess_in_the_friend_reply(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139)
    _coach_said(user, "roughly when r u up and when do u crash")
    sms_capture.clear()
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        seen["system"] = kw["system"]
        return "ima guess up around 11 and down by 2, fix it anytime. u lifting already"
    anthropic_stub.reply_with(_handler)

    assert oa.handle_onboarding_reply(user, "Yeah like staying up hella late and waking up late too") is False
    row = _row(db, user.id)
    assert row.sleep_estimated is True and row.wake_time == "11:00"
    assert "code pinned a guess: up around 11, down around 2" in seen["instruction"]
    assert "do NOT ask about sleep again" in seen["instruction"]
    assert "wake_sleep" not in [f[0] for f in oa._get_missing_fields(row)], "it no longer blocks"
    sys_text = str(seen["system"])
    assert "Wake time: 11:00 (your estimate" in sys_text


def test_sleep_estimate_never_overwrites_a_real_time(db):
    import onboarding_agent as oa
    user = _new_signup(db, wake_time="08:00")
    assert oa.pin_sleep_estimate(user.id, "late") is None
    assert _row(db, user.id).sleep_time is None and _row(db, user.id).sleep_estimated in (False, None)


# ── 3. the big ask in plain words ─────────────────────────────────────────────

def test_big_ask_is_plain_groups_the_gym_bits_and_never_asks_sleep(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student",
                       activity_level="active", cooking_situation="cook", diet="omnivore", existing_tools="none")
    missing = oa._get_missing_fields(user)
    assert {f[0] for f in missing} == {"avg_steps", "workout_days", "workout_time", "current_split", "injuries", "wake_sleep"}
    seen = {}
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.setdefault("i", kw["messages"][0]["content"]) and "ok"))
    oa._build_big_ask_message(user, "yeah i have one", oa._build_system_prompt(user), missing)
    ins = seen["i"]
    assert "about the gym: " in ins and "what split u run and which days u hit what" in ins
    assert "anything that hurts or u gotta work around" in ins
    assert "and separately: roughly how many steps u get in a day" in ins
    assert "when u crash" not in ins and "wake" not in ins.lower(), "sleep is its own question"
    assert "steps are not a gym question" in ins


def test_bundle_uses_the_plain_phrasings(db, anthropic_stub):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2)
    seen = {}
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.setdefault("i", kw["messages"][0]["content"]) and "ok"))
    oa._bundle_gap_questions([("current_split", "x — y"), ("wake_sleep", "x — y")], user, "ok", oa._build_system_prompt(user))
    assert "what split u run and which days u hit what and roughly when ur up and when u crash" in seen["i"]


# ── 4 + 5. the prompt and the voice ───────────────────────────────────────────

def test_prompt_assumes_a_berkeley_student_and_only_asks_what_is_unknown(db):
    import onboarding_agent as oa
    sp = oa._build_system_prompt(_new_signup(db))
    assert "im assuming ur a student here? what major, and any part time job" in sp
    assert "A question that isn't on STILL UNKNOWN is a wasted text" in sp
    assert "Never ask the same thing twice" in sp and "ONE ask, as a rough range" in sp
    assert '"real meals or quick stuff" means nothing' in sp
    # occupation's own hint carries the assumption; wake/sleep's says once
    assert "ASSUME a UC Berkeley student" in sp and "ask ONCE, as a rough range" in sp


def test_identity_bans_the_words_and_knows_they_are_broke():
    from agent_loop import identity_prompt
    ident = identity_prompt()
    assert '"parked"' in ident and '"dragging"' in ident and '"real" as a whole reaction' in ident
    assert "They're broke students." in ident and "double\n  chicken bowl" in ident
    assert "Assume, then say so." in ident
    assert "lwk" in ident, "lwk stays allowed (founder 2026-10-09)"

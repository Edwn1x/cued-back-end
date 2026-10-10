"""Burst replay — a three-text burst is answered once, not three times.

Live 2026-10-10 02:33–02:35 PT (user 49, the founder's from-zero run), msgs 6219–6228:
  02:33:45 "No I mean like 10k daily on average a week"   → turn A (flushed 02:33:52)
  02:33:55 "And idk my sleep schedule  is cooked"          → held (continuation)
  02:34:03 "Like look at the time rn"                       → held (continuation, late)
Turn A's model read all three in the thread and answered them — and pegged sleep itself
("up around 11, down around 3"; code hadn't classified, it only read the first text).
Then the held texts were replayed: turn B pinned 11/2 in code and sent a SECOND peg line
13s after the first, with different numbers, then the summary and the rundown; turn C
(58s old by then, right after the rundown's "ask me anything") went through the coach
loop with no idea it was a continuation: "it's late. go sleep, we can do all this tomorrow."

Fix: the code sleep classifier reads the whole unanswered burst (pinned and stated once,
by code, in turn A); a continuation turn that completes onboarding skips the reaction
bubble; the coach loop gets a CONTINUATION block and turns [silent] into a 👍.
"""
from __future__ import annotations

import json
import logging

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _row, _coach_said, reactions  # noqa: F401

LIVE_6222 = ("10k daily is actually a lot, that bumps u up from what i figured. and yeah cs sleep is "
             "always cooked lol — ima just peg u at like up around 11, down around 3, fix it whenever.")
LIVE_6224 = ("ok so far this is what i have: ur 5'6 141, training 4 days a week, evenings or afternoons, "
             "up around 11 down around 2 (my guess, fix it anytime), tryna lose fat while building muscle")


def _they_said(user, *texts):
    from models import get_session, Message
    s = get_session()
    try:
        for t in texts:
            s.add(Message(user_id=user.id, direction="in", body=t, message_type="freeform"))
        s.commit()
    finally:
        s.close()


def _all_but_sleep(db):
    return _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=141, occupation="student",
                       activity_level="active", avg_steps=10000, workout_days="4", workout_time="18:00",
                       current_split="ppl", cooking_situation="cook", diet="omnivore", injuries="none",
                       existing_tools="none")


# ── 1. onboarding: the code classifier reads the whole burst ─────────────────

def test_sleep_classifier_reads_the_held_texts_of_the_burst(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _all_but_sleep(db)
    assert [f[0] for f in oa._get_missing_fields(user)] == ["wake_sleep"]
    _coach_said(user, "10k a week is like what, 1400 a day. last thing — roughly when ur up and when u crash")
    # the burst: the text this turn flushed for, then the two the buffer is holding
    _they_said(user, "No I mean like 10k daily on average a week",
               "And idk my sleep schedule  is cooked", "Like look at the time rn")
    sms_capture.clear()
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "10k daily is a lot. ima guess up around 11 and down by 2, fix it anytime"
    anthropic_stub.reply_with(_handler)

    done = oa.handle_onboarding_reply(user, "No I mean like 10k daily on average a week")
    row = _row(db, user.id)
    assert (row.wake_time, row.sleep_time, row.sleep_estimated) == ("11:00", "02:00", True), \
        "the vague sleep answer was in a HELD text — code still pins it in the turn that answers the burst"
    assert done is True
    assert "code pinned a guess: up around 11" in seen["instruction"]
    summary = [b for b in _bodies(sms_capture) if b.startswith("ok so")][0]
    assert "up around 11 down around 2 (my guess, fix it anytime)" in summary


def test_burst_scan_off_reads_only_the_flushed_text(db, anthropic_stub, sms_capture, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_BURST_SCAN_ENABLED", False)
    user = _all_but_sleep(db)
    _coach_said(user, "last thing — roughly when ur up and when u crash")
    _they_said(user, "No I mean like 10k daily on average a week", "And idk my sleep schedule  is cooked")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "10k daily is a lot")

    assert oa.handle_onboarding_reply(user, "No I mean like 10k daily on average a week") is False
    row = _row(db, user.id)
    assert row.sleep_time is None and row.wake_time is None


def test_unanswered_burst_is_the_inbounds_since_the_last_coach_text(db):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2)
    _they_said(user, "earlier text")
    _coach_said(user, "what split u run")
    assert oa._unanswered_burst(user.id) == ""
    _they_said(user, "ppl", "wait no, upper lower")
    assert oa._unanswered_burst(user.id) == "ppl\nwait no, upper lower"


def test_the_model_is_told_not_to_guess_sleep_times(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=141)
    _coach_said(user, "what split u run")
    sms_capture.clear()
    seen = []
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.append(kw["messages"][0]["content"]) or "ok"))
    oa.handle_onboarding_reply(user, "ppl")
    assert any("Never invent wake/sleep clock times" in s for s in seen)


# ── 2. onboarding: a continuation that completes sends the summary only ───────

def test_continuation_completion_skips_the_reaction_bubble(db, anthropic_stub, sms_capture, caplog):
    import onboarding_agent as oa
    user = _all_but_sleep(db)
    _coach_said(user, LIVE_6222)          # turn A already reacted (and, live, pegged sleep itself)
    _they_said(user, "And idk my sleep schedule  is cooked")
    sms_capture.clear()
    generated = []
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (generated.append(kw) or "yeah cs sleep is always cooked lol. ima just peg u at up around 11, down around 2"))
    caplog.set_level(logging.INFO)

    done = oa.handle_onboarding_reply(user, "And idk my sleep schedule  is cooked", continuation=True)
    assert done is True
    row = _row(db, user.id)
    assert (row.wake_time, row.sleep_time) == ("11:00", "02:00")
    bodies = _bodies(sms_capture)
    assert bodies[0].startswith("ok so") and "up around 11 down around 2 (my guess" in bodies[0], bodies
    assert not any("peg u at" in b for b in bodies), "no second peg line — the previous reply was the reaction"
    assert generated == [], "the completion reaction was never generated"
    assert "ONBOARDING_CONTINUATION_COMPLETION" in caplog.text and "reaction=skipped" in caplog.text


def test_fresh_turn_completion_keeps_the_reaction_bubble(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _all_but_sleep(db)
    _coach_said(user, "last thing — roughly when ur up and when u crash")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ima guess up around 11 and down by 2, fix it anytime")
    assert oa.handle_onboarding_reply(user, "And idk, sleep schedule is cooked") is True
    bodies = _bodies(sms_capture)
    assert bodies[0] == "ima guess up around 11 and down by 2, fix it anytime", "a fresh turn keeps the stated-guess bubble"
    assert bodies[1].startswith("ok so")


# ── 3. the coach loop: a CONTINUATION block, [silent] → 👍 ─────────────────────

def _blob(kw) -> str:
    return (json.dumps(kw.get("system"), default=str, ensure_ascii=False)
            + json.dumps(kw["messages"][0]["content"], default=str, ensure_ascii=False))


def _done_user(db):
    u = make_user(db, onboarding_step=3, name="Nau")
    _coach_said(u, LIVE_6224)
    _they_said(u, "Like look at the time rn")
    return u


def test_continuation_block_quotes_the_reply_that_already_went(db):
    from agent_loop import _continuation_block, CONTINUATION_HDR
    from models import get_session, User
    u = make_user(db, onboarding_step=3, name="Nau")
    s = get_session()
    try:
        row = s.get(User, u.id)
        blk = _continuation_block(row, "Like look at the time rn", s)
        assert blk.startswith(CONTINUATION_HDR) and "NOT a fresh prompt" in blk and "[silent]" in blk
        assert "ur last reply went out AFTER it" not in blk, "nothing sent yet → nothing to quote"
        _coach_said(u, LIVE_6224)
        blk = _continuation_block(row, "Like look at the time rn", s)
        assert "ur last reply went out AFTER it" in blk and "up around 11 down around 2" in blk
    finally:
        s.close()


def test_loop_continuation_silent_becomes_a_thumbs(db, anthropic_stub, reactions, sms_capture):
    from agent_loop import run_agent_loop, CONTINUATION_HDR
    from models import get_session, User
    u = _done_user(db)
    seen = []

    def _handler(kw):
        if not kw.get("tools"):
            return "freeform"
        seen.append(_blob(kw))
        return "[silent]"
    anthropic_stub.reply_with(_handler)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, u.id), "Like look at the time rn", "freeform", continuation=True)
    finally:
        s.close()
    assert reply == ""
    assert reactions == [(u.id, "like")]
    assert seen and CONTINUATION_HDR in seen[0] and "up around 11 down around 2" in seen[0]


def test_loop_fresh_turn_has_no_block_and_answers(db, anthropic_stub, reactions, sms_capture):
    from agent_loop import run_agent_loop, CONTINUATION_HDR
    from models import get_session, User
    u = _done_user(db)
    seen = []

    def _handler(kw):
        if not kw.get("tools"):
            return "freeform"
        seen.append(_blob(kw))
        return "It's late. Go sleep, we can do all this tomorrow."
    anthropic_stub.reply_with(_handler)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, u.id), "Like look at the time rn", "freeform")
    finally:
        s.close()
    assert reply == "it's late. go sleep, we can do all this tomorrow."
    assert reactions == [] and CONTINUATION_HDR not in seen[0]


def test_loop_block_flag_off_is_inert(db, anthropic_stub, reactions, sms_capture, monkeypatch):
    from agent_loop import run_agent_loop, CONTINUATION_HDR
    from models import get_session, User
    monkeypatch.setattr(config, "CONTINUATION_LOOP_BLOCK_ENABLED", False)
    u = _done_user(db)
    seen = []

    def _handler(kw):
        if not kw.get("tools"):
            return "freeform"
        seen.append(_blob(kw))
        return "[silent]"
    anthropic_stub.reply_with(_handler)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, u.id), "Like look at the time rn", "freeform", continuation=True)
    finally:
        s.close()
    assert CONTINUATION_HDR not in seen[0]
    assert reactions == [], "code doesn't react on its own when the block is off"
    assert reply == ""


def test_app_passes_continuation_into_the_loop(db, monkeypatch, sms_capture):
    import app as appmod
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    u = make_user(db, onboarding_step=3, name="Nau")
    got = {}

    def _fake(user, body, mt, **kw):
        got.update(kw, body=body)
        return "ok"
    monkeypatch.setattr(appmod, "run_agent_loop", _fake)
    appmod.process_buffered_message(u.id, "Like look at the time rn", "freeform", continuation=True)
    assert got.get("continuation") is True and got["body"] == "Like look at the time rn"
    got.clear()
    appmod.process_buffered_message(u.id, "wyd", "freeform")
    assert got.get("continuation") is False

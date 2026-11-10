"""
Plain English for the people who don't text in slang (plain_voice.py).

Live 2026-11-09, user 94: Robert, 67, met the founder in person at a print shop, signed up off
the hero page; the kickoff went out as "hey Robert, it's cued. ur in. quick setup before i can
actually help: gonna get to know u a bit…". Founder: "for older folks, like robert, we drop the
whole slang spelling and all of that. we type out normally and text like a normal regular person."

1. The gate: a hand-set users.plain_voice wins; otherwise age >= PLAIN_VOICE_MIN_AGE; flag off = nobody.
2. plainify: the re-spelling is right on the templates that actually go out, idempotent, and
   leaves URLs / ALLCAPS / numbers / --- alone.
3. The wiring: every send_sms to them is re-spelled (both channels, the SMS invite too); the
   coach-loop context and the onboarding prompt carry the voice block; the lowercase pass is off
   for them; a 20-year-old sees none of it.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
import plain_voice as pv
from plain_voice import plainify, plain_voice
from tests.factories import make_user


def test_knob_defaults():
    assert config.PLAIN_VOICE_ENABLED is True
    assert config.PLAIN_VOICE_MIN_AGE == 40


# ─── 1. the gate ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("age,override,expected", [
    (67, None, True), (40, None, True), (39, None, False), (28, None, False), (19, None, False), (None, None, False),
    (19, True, True), (67, False, False),
])
def test_gate(age, override, expected):
    assert plain_voice(NS(age=age, plain_voice=override)) is expected


def test_gate_off_is_nobody(monkeypatch):
    monkeypatch.setattr(config, "PLAIN_VOICE_ENABLED", False)
    assert plain_voice(NS(age=67, plain_voice=True)) is False


def test_age_floor_zero_means_by_hand_only(monkeypatch):
    monkeypatch.setattr(config, "PLAIN_VOICE_MIN_AGE", 0)
    assert plain_voice(NS(age=67, plain_voice=None)) is False
    assert plain_voice(NS(age=67, plain_voice=True)) is True


def test_gate_from_user_id(db):
    robert = make_user(db, name="Robert", age=67)
    kid = make_user(db, name="Jonah", age=21)
    assert pv.plain_voice_for(robert.id) is True
    assert pv.plain_voice_for(kid.id) is False
    assert pv.plain_voice_for(None) is False
    assert pv.plain_voice_for(999999) is False


# ─── 2. the re-spelling ───────────────────────────────────────────────────────

@pytest.mark.parametrize("before,after", [
    # the kickoff Robert actually got
    ("hey Robert, it's cued. ur in. quick setup before i can actually help: gonna get to know u a bit over the "
     "next few texts, then run u through everything i can do. how's ur day going",
     "Hey Robert, it's cued. You're in. Quick setup before I can actually help: going to get to know you a bit "
     "over the next few texts, then run you through everything I can do. How's your day going"),
    # the SMS invite under it
    ("ps - i can text you on iMessage instead. tap this once and say hey: https://spectrum.photon.codes/users/abc/redirect?msg=hey%20cued",
     "P.S. - I can text you on iMessage instead. Tap this once and say hey: https://spectrum.photon.codes/users/abc/redirect?msg=hey%20cued"),
    # ur: you're before a state, your before a thing
    ("ur at 2510. protein's way over", "You're at 2510. Protein's way over"),
    ("ur calendar's connected. ur week looks open", "Your calendar's connected. Your week looks open"),
    ("ur making progress, ur morning rundown lands when u wake", "You're making progress, your morning rundown lands when you wake"),
    ("ur training days are set", "Your training days are set"),
    ("ur a beginner so ur set for now", "You're a beginner so you're set for now"),
    ("idk where ur", "I don't know where you are"),
    ("legs after CE 93, ur out at 5", "Legs after CE 93, you're out at 5"),
    # the rest of the register
    ("u eat yet today", "You eat yet today"),
    ("im assuming ur a berkeley student? what major", "I'm assuming you're a Berkeley student? What major"),
    ("ngl thats solid. lmk when ur free tmr", "Honestly that's solid. Let me know when you're free tomorrow"),
    ("rsf is dead rn, go", "RSF is dead right now, go"),
    ("nah ur good. u don't need to know anything, that's my job", "No you're good. You don't need to know anything, that's my job"),
    ("1450 and 137g. or lighter, up to u", "1450 and 137g. Or lighter, up to you"),
    ("wyd, cooking or dining hall", "What are you up to, cooking or dining hall"),
    ("ok. u'll see the card when ur at the gym", "Ok. You'll see the card when you're at the gym"),
    ("i'll check. w/ or w/o the whey?", "I'll check. With or without the whey?"),
    ("on ur training days i'll ping u when rsf is quiet so u walk into an empty floor",
     "On your training days I'll ping you when RSF is quiet so you walk into an empty floor"),
    ("i'm on it, i've got the numbers, i'd eat first", "I'm on it, I've got the numbers, I'd eat first"),
    ("also, what u do for work?", "Also, what you do for work?"),
    # bubbles, newlines, ALLCAPS, acronyms, numbers, emoji: untouched where they should be
    ("got it\n---\nu want the numbers?", "Got it\n---\nYou want the numbers?"),
    ("HW4 is due fri. RSF is packed", "HW4 is due fri. RSF is packed"),
    ("tap this: https://app.cued.fit/c/gcal/AbC", "Tap this: https://app.cued.fit/c/gcal/AbC"),
    ("👍 ur in", "👍 you're in"),
    ("", ""),
])
def test_plainify(before, after):
    assert plainify(before) == after
    assert plainify(after) == after          # idempotent


def test_plainify_leaves_standard_english_alone():
    s = "Hey Robert, your calendar is connected. I'll send the card when you're at the gym. Don't stress it."
    assert plainify(s) == s


def test_for_user_is_the_gate_plus_the_pass():
    assert pv.for_user(NS(age=67, plain_voice=None), "ur in") == "You're in"
    assert pv.for_user(NS(age=20, plain_voice=None), "ur in") == "ur in"


# ─── 3. the wiring ────────────────────────────────────────────────────────────

def _capture_sends(monkeypatch):
    import sms
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(sms.time, "sleep", lambda *_: None)
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body, reply_to=None: (sent.append(("imessage", body)), f"pm-{len(sent)}")[1])
    monkeypatch.setattr(sms, "_send_single", lambda phone, body: (sent.append(("sms", body)), f"SM{len(sent)}")[1])
    monkeypatch.setattr(sms, "_log_message", lambda *a, **k: None)
    monkeypatch.setattr(sms, "_is_duplicate_send", lambda *a, **k: False)
    return sent


def test_send_sms_respells_for_robert_over_sms(db, monkeypatch):
    import sms
    robert = make_user(db, name="Robert", age=67)
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "sms")
    sent = _capture_sends(monkeypatch)
    sms.send_sms(robert.phone, "hey Robert, it's cued. ur in. how's ur day going", user_id=robert.id, message_type="onboarding")
    assert sent and sent[0][0] == "sms"
    assert sent[0][1].startswith("Hey Robert, it's cued. You're in. How's your day going")


def test_send_sms_respells_for_robert_over_imessage_bubbles(db, monkeypatch):
    import sms
    robert = make_user(db, name="Robert", age=67)
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    monkeypatch.setattr(sms, "_hold_eligible", lambda uid: False)
    sent = _capture_sends(monkeypatch)
    sms.send_sms(robert.phone, "got it\n---\nu want the numbers rn?", user_id=robert.id, message_type="freeform")
    assert [b for c, b in sent if c == "imessage"] == ["Got it", "You want the numbers right now?"]


def test_send_sms_leaves_a_student_alone(db, monkeypatch):
    import sms
    kid = make_user(db, name="Jonah", age=21)
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "sms")
    sent = _capture_sends(monkeypatch)
    sms.send_sms(kid.phone, "ur in. how's ur day going", user_id=kid.id, message_type="onboarding")
    assert sent[0][1].startswith("ur in. how's ur day going")


def test_imessage_invite_is_plain_for_robert(db, monkeypatch):
    import sms
    robert = make_user(db, name="Robert", age=67)
    monkeypatch.setattr("photon.imessage_link_for_user", lambda uid: "https://spectrum.photon.codes/users/abc/redirect?msg=hey%20cued")
    out = sms._with_imessage_invite(robert.id, "Hey Robert, you're in.")
    assert "P.S. — I can text you on iMessage instead. Tap this once" in out
    assert "https://spectrum.photon.codes/users/abc/redirect?msg=hey%20cued" in out


def test_loop_context_carries_the_voice_block_for_robert(db):
    from agent_loop import build_loop_context
    robert = make_user(db, name="Robert", age=67)
    ctx = build_loop_context(robert, db)
    assert "PLAIN ENGLISH" in ctx and "They are 67" in ctx
    assert ctx.index("PLAIN ENGLISH") < 200          # the first block after the cached prefix


def test_loop_context_has_no_voice_block_for_a_student(db):
    from agent_loop import build_loop_context
    kid = make_user(db, name="Jonah", age=21)
    assert "PLAIN ENGLISH" not in build_loop_context(kid, db)


def test_onboarding_prompt_carries_the_voice_block_for_robert(db):
    import onboarding_agent as oa
    robert = make_user(db, name="Robert", age=67, onboarding_step=1)
    prompt = oa._build_system_prompt(robert)
    assert "PLAIN ENGLISH" in prompt
    # and (PR #319) a 67-year-old is not assumed a Berkeley student
    assert "NOT that they're a student" in prompt
    kid = make_user(db, name="Jonah", age=21, onboarding_step=1)
    assert "PLAIN ENGLISH" not in oa._build_system_prompt(kid)


def test_hand_set_override_beats_age(db):
    from agent_loop import build_loop_context
    young_plain = make_user(db, name="Pat", age=24, plain_voice=True)
    old_slang = make_user(db, name="Sam", age=55, plain_voice=False)
    assert "PLAIN ENGLISH" in build_loop_context(young_plain, db)
    assert "PLAIN ENGLISH" not in build_loop_context(old_slang, db)

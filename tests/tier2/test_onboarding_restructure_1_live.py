"""
Tier-2 (live) anchors for onboarding restructure PR 1 (2026-10-09). Binary, x3 each
(founder rule). The founder's own run (user 48, 2026-10-05) is the script.

  1. Continuation: text 2 of a burst lands after the reply to text 1 already went →
     the model says nothing ([skip]) or one line with no question; never the same
     question in other words (live 5785/5786).
  2. Vague sleep answer → the reply states the pinned guess (11 / 2) and asks nothing
     about sleep (live 5800-5803: three clarifiers).
  3. First reply to "It's going good" with occupation unknown → the stated Berkeley
     assumption, not "student or working".
  4. The big ask names the split in plain words and never asks about sleep.
Run: pytest tests/tier2/test_onboarding_restructure_1_live.py --run-tier2 -s
"""
from __future__ import annotations

import re

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE, _new_signup

pytestmark = pytest.mark.tier2

N = 3
LIVE_5785 = "real. do u cook in ur room or is it dining hall mostly"
SLEEP_Q_RE = re.compile(r"\b(when|what time|which way|how late|cooked how)\b.*\?|\?.*\b(sleep|wake|crash|bed)\b|\b(sleep|wake|crash|bed)\b.*\?", re.I)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for f in ("ONBOARDING_RUNDOWN_ENABLED", "CARD_SETUP_ENABLED", "WATER_OFFER_ENABLED", "PHOTON_PROVISIONING_ENABLED"):
        monkeypatch.setattr(config, f, False)
    monkeypatch.setattr(config, "ONBOARDING_CONTINUATION_FOLD_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_SLEEP_ESTIMATE_ENABLED", True)


def _coach_said(user, text, mtype="onboarding"):
    from sms import send_sms
    send_sms(user.phone, text, user_id=user.id, message_type=mtype)


def _edwin(db, **over):
    kw = dict(INTAKE, name="edwin", age=20, gender="male", goal="fat_loss,muscle_building,strength",
              experience="intermediate", equipment="full_gym", onboarding_step=2,
              height_ft=5, height_in=6, weight_lbs=139, workout_days="4", workout_time="18:00", diet="omnivore")
    kw.update(over)
    return make_user(db, **kw)


def test_continuation_never_reasks_the_question(db, sms_capture):
    import onboarding_agent as oa
    out = []
    for i in range(N):
        u = _edwin(db, occupation="cs student at berkeley")
        _coach_said(u, "hey edwin, it's cued. ur spot's open. gonna get to know u a bit over the next few texts, then ur first workout. how's ur day going")
        _coach_said(u, "cs at berkeley is no joke. you walking all over campus between classes or mostly in a lab")
        _coach_said(u, LIVE_5785)
        sms_capture.clear()
        oa.handle_onboarding_reply(u, "I'm either walking somewhere or in my room", continuation=True)
        sent = [b for _p, b in sms_capture]
        out.append(sent)
        print(f"\n[continuation {i+1}] sent={sent!r}")
        assert len(sent) <= 1
        if sent:
            assert "?" not in sent[0], sent
            assert not oa.is_duplicate_question(sent[0], LIVE_5785), sent
            assert not re.search(r"\b(cook|dining hall)\b.*\b(or)\b", sent[0], re.I), sent


def test_vague_sleep_answer_gets_the_stated_guess_and_no_question(db, sms_capture):
    import onboarding_agent as oa
    for i in range(N):
        u = _edwin(db, occupation="cs student at berkeley", activity_level="active", avg_steps=10000,
                   current_split="ppl", cooking_situation="cook", injuries="none", existing_tools="none")
        _coach_said(u, "roughly when r u up and when do u crash")
        sms_capture.clear()
        oa.handle_onboarding_reply(u, "And idk, sleep schedule is cooked")
        sent = [b for _p, b in sms_capture]
        print(f"\n[sleep {i+1}] sent={sent!r}")
        bubble = sent[0]
        assert re.search(r"\b11\b", bubble) and re.search(r"\b2\b", bubble), bubble
        assert not SLEEP_Q_RE.search(bubble), bubble
        assert any(b.startswith("ok so") and "(my guess, fix it anytime)" in b for b in sent), sent


def test_first_reply_states_the_berkeley_assumption(db, sms_capture):
    import onboarding_agent as oa
    for i in range(N):
        u = _edwin(db, onboarding_step=1)
        _coach_said(u, "hey edwin, it's cued. ur spot's open. gonna get to know u a bit over the next few texts, then ur first workout. how's ur day going")
        sms_capture.clear()
        oa.handle_onboarding_reply(u, "It's going good")
        sent = [b for _p, b in sms_capture]
        print(f"\n[assumption {i+1}] sent={sent!r}")
        body = " ".join(sent).lower()
        assert "assum" in body or "guess" in body, sent
        assert "student" in body and "major" in body, sent
        assert "student or working" not in body and "dragging" not in body and "parked" not in body, sent


def test_big_ask_is_plain_and_skips_sleep(db, sms_capture):
    import onboarding_agent as oa
    for i in range(N):
        u = _edwin(db, occupation="cs student at berkeley", activity_level="active", cooking_situation="cook",
                   existing_tools="none")
        missing = oa._get_missing_fields(u)
        text = oa._build_big_ask_message(u, "Yeah I have one", oa._build_system_prompt(u), missing)
        print(f"\n[bigask {i+1}] {text!r}")
        low = text.lower()
        assert "split" in low, text
        assert not re.search(r"\b(wake|crash|sleep|bed)\b", low), text
        assert "group together" not in low, text

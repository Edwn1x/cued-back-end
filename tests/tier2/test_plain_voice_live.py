"""
Tier-2 (live) anchors for plain voice (2026-11-09, user 94: Robert, 67). Binary, x3.

  1. Robert answers the hook in standard English → the MODEL's own reply (before the code
     re-spelling) is standard English too: no u / ur / rn / ngl, a capital letter opens it, no
     Berkeley, at most one question. And what actually goes out is the same or better.
  2. Post-setup, a plain lunch question gets a plain answer from the coach loop.
Run: pytest tests/tier2/test_plain_voice_live.py --run-tier2 -s
"""
from __future__ import annotations

import re

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE

pytestmark = pytest.mark.tier2

CAMPUS_RE = re.compile(r"\b(berkeley|student|major|campus|class(es)?|dining hall|rsf|midterm|semester|dorm)\b", re.I)
SLANG_RE = re.compile(r"\b(u|ur|r|rn|ngl|tbh|idk|ig|lmk|tmr|tmrw|gonna|wanna|ima|im|hella|wyd|wya|alr|mb|lw|lwk|atp|fr|prolly|bruhh?|lol)\b")
HOOK = ("hey Robert, it's cued. ur in. quick setup before i can actually help: gonna get to know u a bit over "
        "the next few texts, then run u through everything i can do. how's ur day going")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for f in ("ONBOARDING_RUNDOWN_ENABLED", "CARD_SETUP_ENABLED", "WATER_OFFER_ENABLED", "PHOTON_PROVISIONING_ENABLED"):
        monkeypatch.setattr(config, f, False)


def _robert(db, **over):
    kw = dict(INTAKE, name="Robert", age=67, gender="male", goal="fat_loss,muscle_building,strength,general_fitness",
              biggest_obstacle="consistency", experience="none", equipment="full_gym", diet="omnivore",
              workout_time="08:00", workout_days="4", height_ft=5, height_in=11, weight_lbs=205,
              existing_tools="none", onboarding_step=1, reported_intake=None, occupation=None)
    kw.update(over)
    return make_user(db, **kw)


def _first_alpha_upper(s: str) -> bool:
    for ch in s:
        if ch.isalpha():
            return ch.isupper()
    return True


@pytest.mark.parametrize("first", ["Doing fine, thanks. Just got back from the hardware store.", "Good. Quiet day so far."])
def test_robert_gets_plain_english_from_the_model_itself(db, sms_capture, monkeypatch, first):
    import onboarding_agent as oa
    import plain_voice
    from sms import send_sms
    raw: list[str] = []
    real = plain_voice.plainify

    def _spy(text):
        raw.append(text)
        return real(text)
    monkeypatch.setattr(plain_voice, "plainify", _spy)
    for i in range(3):
        u = _robert(db)
        send_sms(u.phone, HOOK, user_id=u.id, message_type="onboarding")
        sms_capture.clear(); raw.clear()
        oa.handle_onboarding_reply(u, first)
        sent = [b for _p, b in sms_capture]
        print(f"\n[{first!r} {i+1}] model={raw!r}\n   sent={sent!r}")
        body = " ".join(sent)
        assert sent and not CAMPUS_RE.search(body), sent
        assert body.count("?") <= 1, sent
        # the model wrote standard English on its own — the code pass had little or nothing to do
        model_text = " ".join(raw)
        assert not SLANG_RE.search(model_text), raw
        assert all(_first_alpha_upper(b) for b in model_text.split("\n") if b.strip() and b.strip() != "---"), raw
        # and what went out is standard English, full stop
        assert not SLANG_RE.search(body), sent
        assert all(_first_alpha_upper(b) for b in sent), sent


def test_robert_post_setup_lunch_question_is_plain(db, monkeypatch):
    from agent_loop import run_agent_loop
    for i in range(3):
        u = _robert(db, onboarding_step=3, occupation="retired", coaching_summary="Robert, 67, retired, beginner. Fat loss + strength. 4 days at 8am.")
        reply = run_agent_loop(u, "What should I have for lunch? I have chicken and rice at home.", "freeform")
        print(f"\n[lunch {i+1}] reply={reply!r}")
        assert reply and not SLANG_RE.search(reply), reply
        assert all(_first_alpha_upper(b) for b in reply.split("\n") if b.strip() and b.strip() != "---"), reply
        assert not CAMPUS_RE.search(reply), reply

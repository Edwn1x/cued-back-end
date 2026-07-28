"""
Image-fact persistence (burn-in fix — the chicken-tenders bug). A fact that appears
only in an image must reach durable state through a tool call in the SAME turn;
reading it into the reply persists nothing. Tier-1 pins the deterministic parts:
the [image attached] marker on inbound Message rows (new behavior, red-first), the
remember/log_meal/log_event write paths from an image turn (regression guards), and
prompt-shape tripwires so a future voice.md edit can't silently revert the routing.
The model's actual compliance (does it CALL remember on a package photo?) is tier-2:
tests/tier2/test_image_fact_persistence.py.
"""

from __future__ import annotations

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}

TENDERS_FACT = "has a 1.5 lb (680 g) package of chicken tenders on hand, uncooked — not eaten yet"


def _profile(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return dict(s.get(User, user_id).user_profile_memory or {})
    finally:
        s.close()


def _fresh_context(user_id):
    from models import get_session, User
    from agent_loop import build_loop_context
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def _wipe_messages(user_id):
    """Simulate the conversation window rolling past the image turn entirely."""
    from models import get_session, Message
    s = get_session()
    try:
        s.query(Message).filter(Message.user_id == user_id).delete()
        s.commit()
    finally:
        s.close()


# ── the [image attached] marker (fix: the window must retain a trace) ────────────


def test_log_incoming_marks_image_attachment(db):
    from tests.factories import make_user
    from sms import log_incoming, IMAGE_MARKER
    from models import get_session, Message

    user = make_user(db)
    log_incoming(user.id, "check this out", has_image=True)
    log_incoming(user.id, "", has_image=True)           # captionless MMS
    log_incoming(user.id, "plain text", has_image=False)

    s = get_session()
    try:
        bodies = [m.body for m in s.query(Message)
                  .filter(Message.user_id == user.id).order_by(Message.id).all()]
    finally:
        s.close()
    assert bodies[0] == f"check this out {IMAGE_MARKER}"
    assert bodies[1] == IMAGE_MARKER, "captionless image must still leave a trace"
    assert bodies[2] == "plain text", "no marker without an image (default unchanged)"


def test_webhook_logs_inbound_with_image_marker(client, db, monkeypatch):
    """MMS through the real webhook → the logged inbound row carries the marker,
    so a later turn can honestly say 'you sent a pic earlier'."""
    from tests.factories import make_user
    from sms import IMAGE_MARKER
    from models import get_session, Message

    user = make_user(db)

    class _FakeImg:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}
        content = b"\xff\xd8fakejpeg"

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeImg())

    resp = client.post("/webhook", data={
        "From": user.phone, "Body": "look at this",
        "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/fake/media0",
        "MessageSid": "SMimgmarker",
    })
    assert resp.status_code == 200

    s = get_session()
    try:
        inbound = (s.query(Message)
                   .filter(Message.user_id == user.id, Message.direction == "in")
                   .order_by(Message.id.desc()).first())
    finally:
        s.close()
    assert inbound is not None
    assert inbound.body == f"look at this {IMAGE_MARKER}"


# ── image-only facts persist via remember and survive window loss ────────────────


def test_image_fact_persists_via_remember_and_survives_window_loss(db, monkeypatch, anthropic_stub):
    """The tenders anchor, tier-1 half: an image turn whose only durable output is a
    loose fact writes it through remember; the fact is still in context after every
    Message row is gone (i.e. it outlives the conversation window)."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)

    anthropic_stub.push(
        ToolUse("remember", {"action": "add", "category": "constraints", "text": TENDERS_FACT}),
        "1.5 lb of tenders on deck — noted. lmk when you cook them",
    )
    user = make_user(db)
    reply = run_agent_loop(user, "", "freeform", image_data=_IMG)
    assert reply, "loop must still produce a reply after the tool turn"

    prof = _profile(user.id)
    assert any(TENDERS_FACT in (e.get("text") or "") for e in prof.get("constraints") or []), \
        "the image-only fact never reached durable state"

    _wipe_messages(user.id)
    ctx = _fresh_context(user.id)
    assert "1.5 lb" in ctx, "stored image fact must survive the window rolling past the turn"


def test_preconsumption_weight_available_to_a_later_turn(db):
    """Pre-consumption case: with the fact stored, a later 'eating the whole thing'
    turn has the weight in its context — resolvable without re-asking."""
    from tests.factories import make_user
    from agent_tools import handle_remember

    user = make_user(db)
    out = handle_remember(user.id, {"action": "add", "category": "constraints",
                                    "text": TENDERS_FACT})
    assert out.startswith("ok"), out

    ctx = _fresh_context(user.id)  # no Message rows at all — pure durable state
    assert "1.5 lb" in ctx and "680 g" in ctx, \
        "the later turn's context must carry the stored weight"
    assert "WHAT YOU REMEMBER" in ctx


# ── regression guards: the existing image routes still write ─────────────────────


def test_food_photo_still_routes_to_log_meal(db, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)

    anthropic_stub.push(
        ToolUse("log_meal", {"description": "chicken bowl", "calories": 650, "protein_g": 48}),
        "logged — 650 cal, 48g protein",
    )
    user = make_user(db)
    run_agent_loop(user, "lunch", "food_photo", image_data=_IMG)

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
    finally:
        s.close()
    assert len(meals) == 1 and meals[0].description == "chicken bowl"


def test_calendar_screenshot_still_routes_to_log_event(db, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from events import todays_events

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)

    anthropic_stub.push(
        ToolUse("log_event", {"description": "orgo midterm", "starts_at": "09:00", "date": "today"}),
        "orgo midterm at 9 — on the calendar",
    )
    user = make_user(db)
    run_agent_loop(user, "", "freeform", image_data=_IMG)

    evs = todays_events(user.id)
    assert len(evs) == 1 and evs[0].raw_text == "orgo midterm"


def test_sleep_screenshot_persists_as_ordinary_facts_no_subsystem(db, monkeypatch, anthropic_stub):
    """Sleep stays out of scope as a FEATURE: a sleep screenshot persists via the same
    remember path as any loose fact, and there is no sleep table to invoke."""
    import config
    import models
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)

    fact = "fitbit summary 7/27: 6h12m sleep, avg ~6h over the week"
    anthropic_stub.push(
        ToolUse("remember", {"action": "add", "category": "identity", "text": fact}),
        "6h average is eating your recovery — let's talk bedtime",
    )
    user = make_user(db)
    run_agent_loop(user, "", "freeform", image_data=_IMG)

    prof = _profile(user.id)
    assert any(fact in (e.get("text") or "") for e in prof.get("identity") or []), \
        "sleep screenshot contents must persist as ordinary facts"
    assert not any("sleep" in t for t in models.Base.metadata.tables), \
        "no sleep subsystem may exist (out of scope)"


# ── prompt-shape tripwires (the fix lives in prompt text; pin it) ────────────────


def test_voice_prompt_pins_image_fact_routing_and_honesty():
    from agent_loop import _voice_prompt
    from sms import IMAGE_MARKER

    voice = _voice_prompt()
    low = voice.lower()
    # the drop instruction is gone
    assert "store nothing structured" not in low, \
        "the 'store nothing structured' bucket is the tenders bug — must not return"
    # pre-consumption branch exists and points at remember
    assert "not yet eaten" in low or "not eaten yet" in low
    # the load-bearing principle is stated
    assert "read" in low and "not saved" in low or "is not saved" in low
    # retrieval-gap honesty: the banned delivery-failure claim is named
    assert "came through" in low, "honesty rule must name the 'never came through' claim"
    assert IMAGE_MARKER in voice, "honesty rule must reference the inbound image marker"


def test_remember_tool_description_covers_image_facts():
    from agent_tools import REMEMBER_TOOL
    desc = REMEMBER_TOOL["description"].lower()
    assert "image" in desc, \
        "remember's description must say image-extracted facts are in-scope (the tenders bug)"

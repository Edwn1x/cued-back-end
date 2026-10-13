"""A connect-link error report, handled honestly (live 2026-10-13 05:12–05:13, user 51).

"I got a 401 error?" on the calendar link → the coach said "my bad — the calendar link's
still being set up on our end" (a status it cannot know), asked "whats the google account
ur calendar's on?" (the Testing-mode flow, obsolete since OAuth was approved), added "go
sleep lol" as a third bubble, and the setup sequence stacked the water offer on top.
"""
from __future__ import annotations

import logging

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_card_setup import sidecar_ok, card_ok, sync_threads, _u  # noqa: F401
from tests.tier1.test_setup_sequence import seq_on, _user, _inbound, _backdate  # noqa: F401
from tests.tier1.test_reactions_and_replies import imessage_on, sidecar, SECRET  # noqa: F401


def _tool_names(anthropic_stub, db, monkeypatch, testing_mode: bool) -> list[str]:
    from agent_loop import run_agent_loop
    from models import get_session, User
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", testing_mode)
    monkeypatch.setattr(config, "SEND_CONNECT_LINK_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    seen = []
    def _h(kw):
        if kw.get("tools"):
            seen.append([t["name"] for t in kw["tools"]])
        return "ok"
    anthropic_stub.reply_with(_h)
    u = make_user(db, onboarding_step=3, preferred_channel="imessage")
    s = get_session()
    try:
        run_agent_loop(s.get(User, u.id), "hey", "freeform")
    finally:
        s.close()
    return seen[0] if seen else []


def test_google_account_tool_only_while_oauth_is_in_testing(db, anthropic_stub, sms_capture, monkeypatch):
    names_off = _tool_names(anthropic_stub, db, monkeypatch, testing_mode=False)
    assert "send_connect_link" in names_off and "set_google_account" not in names_off, names_off


def test_google_account_tool_present_in_testing_mode(db, anthropic_stub, sms_capture, monkeypatch):
    names_on = _tool_names(anthropic_stub, db, monkeypatch, testing_mode=True)
    assert "set_google_account" in names_on, names_on


def test_integrations_block_tells_the_coach_how_to_take_a_link_error(db, monkeypatch):
    from connect_offers import integrations_block
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    u = make_user(db, onboarding_step=3)
    blk = integrations_block(u, None, None)
    assert "never say it's 'still being set up on our end'" in blk
    assert "don't ask which google account it's on" in blk and "send a fresh link in the same turn" in blk


def test_setup_sequence_does_not_advance_on_a_problem_report(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                             anthropic_stub, caplog):
    import onboarding_agent as oa
    import setup_sequence as ss
    anthropic_stub.reply_with(lambda kw: "locked in")
    caplog.set_level(logging.INFO)
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    _inbound(u.id, "I got a 401 error?")
    assert ss.on_inbound(u.id) is None and "SETUP_STEP_DEFERRED" in caplog.text and "problem_report" in caplog.text
    _inbound(u.id, "ok done")
    assert ss.on_inbound(u.id) is not None, "a normal reply advances as before"


# ── a tapback on a setup step advances the sequence ─────────────────────────────

def test_a_reaction_advances_the_setup_sequence_without_an_inbound_row(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                                       anthropic_stub, caplog):
    """Live 2026-10-13 05:31 (user 51): 👍 on "connected. i'll pull ur calendar in…" → nothing.
    A reaction is not a Message row, so on_inbound never ran and the step never read as
    answered; the sweep sat in his quiet hours. The tapback IS the answer."""
    import onboarding_agent as oa
    import setup_sequence as ss
    anthropic_stub.reply_with(lambda kw: "locked in")
    caplog.set_level(logging.INFO)
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    sidecar_ok.clear()
    assert ss.on_inbound(u.id) is None, "no inbound yet → the normal path waits"
    r = ss.on_reaction(u.id)
    assert r and r.startswith("connect:"), r
    assert "SETUP_STEP user=%d step=connect trigger=reaction" % u.id in caplog.text


def test_reaction_webhook_runs_the_setup_step(db, imessage_on, sidecar, client, monkeypatch, sms_capture):
    import json
    import setup_sequence
    from sms import _log_message
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _log_message(user.id, "connected. i'll pull ur calendar in and plan around it", "integration_connected",
                 channel="imessage", provider_sid="spc-connected", delivery_status="sent")
    got = []
    monkeypatch.setattr(setup_sequence, "on_reaction", lambda uid: got.append(uid) or "water:sent")
    r = client.post("/internal/inbound", data=json.dumps({
        "phone": user.phone, "text": "", "provider_message_id": "spc-react-1", "chat_guid": "g", "service": "iMessage",
        "line_phone": "+16282649335", "timestamp": "2026-10-13T12:31:35Z", "attachments": [],
        "reaction": {"emoji": "👍", "target_id": "spc-connected"}}),
        headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200 and got == [user.id], (r.get_json(), got)

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

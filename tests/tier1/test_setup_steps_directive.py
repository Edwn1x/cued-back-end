"""Directive setup steps (founder, 2026-10-12): after the summary the first step goes out as
a thing to do — "first thing: connect ur google calendar … tap this" + the link — no
"quick version" rundown, no "want me on ur calendar? … or ignore this" offer, no waiting
for their next text. Later steps lead with "next:". Food logging rides on the summary
tail. The connect links carry the "your week" card art as their preview picture.
"""
from __future__ import annotations

import logging

import pytest

import config
from tests.tier1.test_card_setup import sidecar_ok, card_ok, sync_threads, _u  # noqa: F401
from tests.tier1.test_setup_sequence import seq_on, _user, _inbound, _backdate  # noqa: F401


@pytest.fixture
def directive(seq_on, monkeypatch):
    monkeypatch.setattr(config, "SETUP_STEPS_DIRECTIVE_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", True)     # would send the rundown if it still ran
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "https://app.example")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)       # prod: OAuth approved 2026-10-12


def test_completion_sends_the_summary_then_the_first_step_as_a_thing_to_do(db, directive, sidecar_ok, card_ok,
                                                                           sync_threads, anthropic_stub, caplog):
    import onboarding_agent as oa
    from connect_offers import STEP_GCAL_LINK
    anthropic_stub.reply_with(lambda kw: "locked in")
    caplog.set_level(logging.INFO)
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    b = sidecar_ok
    assert b[0].startswith("ok so far this is what i have: ") and oa.SUMMARY_FOOD_CLAUSE in b[0], b[0]
    assert b[1] == f"first thing: {STEP_GCAL_LINK}", b[1]
    assert b[2].startswith("https://app.example/c/gcal/"), b[2]
    assert len(b) == 3, b
    assert not any("quick version" in x or "quick rundown" in x for x in b), "no rundown bubble"
    assert "SETUP_STEP user=%d step=connect trigger=completion" % u.id in caplog.text or "trigger=completion" in caplog.text
    assert card_ok["sent"] == []


def test_the_next_step_leads_with_next(db, directive, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    import setup_sequence as ss
    from connect_offers import STEP_BCOURSES
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    sidecar_ok.clear()
    _inbound(u.id, "done")                                    # they answered the calendar step
    assert ss.on_inbound(u.id) == "connect:bcourses"
    assert sidecar_ok == [f"next: {STEP_BCOURSES}"], sidecar_ok


def test_directive_off_keeps_the_rundown_and_the_ask_copy(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                          anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    import setup_sequence as ss
    from connect_offers import OFFER_GCAL_LINK
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", True)
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "https://app.example")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    assert sidecar_ok[0].startswith("ok so far") and oa.SUMMARY_FOOD_CLAUSE not in sidecar_ok[0]
    assert sidecar_ok[1].startswith("quick version: ")
    assert len(sidecar_ok) == 2
    sidecar_ok.clear()
    _inbound(u.id, "ok")
    assert ss.on_inbound(u.id) == "connect:gcal" and sidecar_ok[0] == OFFER_GCAL_LINK


def test_connect_link_preview_uses_the_card_art(monkeypatch):
    from integrations import routes
    monkeypatch.setattr(config, "CONNECT_OG_IMAGE_URL", "")
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "https://app.cued.fit/")
    assert routes._og_image() == "https://app.cued.fit/static/og/connect.png"
    monkeypatch.setattr(config, "CONNECT_OG_IMAGE_URL", "https://cdn.example/x.png")
    assert routes._og_image() == "https://cdn.example/x.png"
    monkeypatch.setattr(config, "CONNECT_OG_IMAGE_URL", "")
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "")
    assert routes._og_image() == routes._OG_IMAGE
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "https://app.cued.fit")
    page = routes._handoff_page("google calendar", "https://accounts.google.com/o/oauth2/auth?x=1").get_data(as_text=True)
    assert "<meta property='og:image' content='https://app.cued.fit/static/og/connect.png'>" in page
    assert "og:image:width' content='840'" in page and "og:image:height' content='1050'" in page


def test_testing_mode_ask_is_directive_too(db, directive, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    from connect_offers import STEP_GCAL_ASK
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", True)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    assert sidecar_ok[1] == f"first thing: {STEP_GCAL_ASK}" and len(sidecar_ok) == 2


def test_the_card_art_is_served_as_a_static_file(client):
    r = client.get("/static/og/connect.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and len(r.data) > 50_000
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(r.data)).size == (840, 1050)

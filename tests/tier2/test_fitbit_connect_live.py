"""
Tier-2 (live) — Fitbit connect anchor (Part 2a). The founder's exact ask, with Google
Calendar ALREADY connected in INTEGRATIONS: does the coach fire send_connect_link with
provider=fitbit (a SECOND one-tap link) rather than telling them "you already connected
google"? The deterministic half (tool → link bubble → pending row) is tier-1; this is the
model-layer seam. Run: pytest --run-tier2 -s tests/tier2/test_fitbit_connect_live.py
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


def _enable(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "GCAL_ENABLED", "FITBIT_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "live-anchor-secret")


def _gcal_connected(db, user_id):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=user_id, provider="gcal", status="connected", meta={}))
        s.commit()
    finally:
        s.close()


@pytest.mark.parametrize("text", [
    "now that i have google connected, can i connect my google fitbit air?",
    "can u see my fitbit sleep? i wanna connect it",
])
def test_fitbit_ask_with_google_connected_sends_the_fitbit_link(db, monkeypatch, sms_capture, text):
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from integrations import base
    user = make_user(db, name="Nau", phone="+15105550199")
    _gcal_connected(db, user.id)
    reply = run_agent_loop(user, text, "freeform")
    links = [b for _p, b in sms_capture if "/c/fitbit?t=" in b]
    print(f"\n[FITBIT ASK] {text!r}\n  -> {reply!r}\n  link bubbles: {len(links)}")
    assert links, f"expected a /c/fitbit link bubble; reply={reply!r}"
    assert base.pending_nonce(user.id, "fitbit"), "pending fitbit row should hold the connect nonce"
    low = (reply or "").lower()
    assert "already connected" not in low and "/c/gcal" not in low, reply

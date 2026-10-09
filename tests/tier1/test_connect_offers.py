"""
Connecting integrations proactively (connect_offers.py, founder 2026-10-02).

1. Testing-mode gate: a Google link needs a known, allowlisted Google account first.
   set_google_account stores it; send_connect_link refuses until the admin marks it;
   the INTEGRATIONS block tells the coach which state it's in.
2. Reconnect nudge: a revoked Google row gets one line + a fresh link, once per revoke.
3. First offers: a day into coaching, one per provider a day apart, once ever — the
   calendar (link, or the account question in Testing), bcourses for students, the
   wearable only if they named one. Allowlisting → the promised link goes out.
4. Everything runs inside heartbeat.guardrail_reason; one action per user per sweep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GOOGLE_OAUTH_TESTING_MODE", "CONNECT_OFFER_ENABLED", "RECONNECT_NUDGE_ENABLED",
              "GCAL_ENABLED", "GOOGLE_HEALTH_ENABLED", "BCOURSES_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "INTEGRATIONS_BASE_URL", "https://app.example")
    import heartbeat
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: None)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


def _integ(uid, provider):
    from models import get_session, Integration
    s = get_session()
    try:
        return s.query(Integration).filter_by(user_id=uid, provider=provider).first()
    finally:
        s.close()


def _onboarded(db, **kw):
    base = dict(onboarding_step=3, activated_at=_now() - timedelta(days=2), created_at=_now() - timedelta(days=3))
    base.update(kw)
    return make_user(db, **base)


def _bodies(sms_capture):
    return [b for _p, b in sms_capture]


# ── 1. the account-first gate ────────────────────────────────────────────────

def test_allowlist_state_matrix(db, monkeypatch):
    import config
    from connect_offers import allowlist_state
    u = _onboarded(db)
    assert allowlist_state(u) == "needs_account"
    u = _onboarded(db, google_email="jane@gmail.com")
    assert allowlist_state(u) == "needs_allowlist"
    u = _onboarded(db, google_email="jane@gmail.com", google_allowlisted_at=_now())
    assert allowlist_state(u) == "ok"
    # ever connected a Google provider → already on the list
    v = _onboarded(db)
    from models import get_session, Integration
    s = get_session(); s.add(Integration(user_id=v.id, provider="gcal", status="revoked", meta={})); s.commit(); s.close()
    assert allowlist_state(_u(v.id)) == "ok"
    # published consent screen → no gate at all
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    assert allowlist_state(_onboarded(db)) == "ok"


def test_send_connect_link_refuses_google_until_allowlisted(db, sms_capture):
    from agent_tools import handle_send_connect_link, handle_set_google_account
    u = _onboarded(db)
    r = handle_send_connect_link(u.id, {"provider": "gcal"})
    assert r.startswith("error:") and "which google account" in r and sms_capture == []

    r = handle_set_google_account(u.id, {"email": "Jane.Doe@Gmail.com "})
    assert r.startswith("ok:") and "not set up on our side yet" in r and "do NOT call send_connect_link" in r
    assert _u(u.id).google_email == "jane.doe@gmail.com"
    r = handle_send_connect_link(u.id, {"provider": "google_health"})
    assert r.startswith("error:") and "jane.doe@gmail.com isn't set up" in r and sms_capture == []

    from connect_offers import mark_allowlisted
    assert mark_allowlisted(u.id) is True
    r = handle_send_connect_link(u.id, {"provider": "gcal"})
    assert r.startswith("ok:") and len(sms_capture) == 1 and "/c/gcal/" in sms_capture[0][1]
    # a second set_google_account with the same address keeps the allowlist stamp
    assert "set up — send the link now" in handle_set_google_account(u.id, {"email": "jane.doe@gmail.com"})
    # a DIFFERENT account needs its own entry
    handle_set_google_account(u.id, {"email": "other@gmail.com"})
    assert _u(u.id).google_allowlisted_at is None


def test_set_google_account_rejects_non_emails(db):
    from agent_tools import handle_set_google_account
    u = _onboarded(db)
    assert handle_set_google_account(u.id, {"email": "my gmail"}).startswith("error:")
    assert _u(u.id).google_email is None


def test_integrations_block_carries_the_account_state(db, monkeypatch):
    from connect_offers import context_line
    import config
    assert "unknown — before any google calendar / fitbit link, ask" in context_line(_onboarded(db))
    assert "not set up on our side yet. Do NOT send a google link" in context_line(_onboarded(db, google_email="a@gmail.com"))
    assert context_line(_onboarded(db, google_email="a@gmail.com", google_allowlisted_at=_now())) == "google account: a@gmail.com — google links work"
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    assert context_line(_onboarded(db)) is None


def test_coach_prompt_shows_the_integrations_block_for_a_fresh_user(db):
    import agent_loop
    u = _onboarded(db)
    sp = agent_loop.build_context(u) if hasattr(agent_loop, "build_context") else None
    if sp is None:
        pytest.skip("no public context builder")
    assert "## INTEGRATIONS" in sp and "gcal: NOT connected" in sp and "google account: unknown" in sp


def test_voice_and_tool_registry(db):
    from agent_loop import _voice_prompt
    v = _voice_prompt()
    assert "Google links need the account first" in v and "set_google_account" in v
    from capabilities import CAPABILITIES
    cap = {c.id: c for c in CAPABILITIES}["connect_accounts"]
    assert "set_google_account" in cap.tools


# ── 2. the reconnect nudge ───────────────────────────────────────────────────

def test_revoked_google_row_gets_one_nudge_per_revoke(db, sms_capture):
    from models import get_session, Integration
    from integrations import base
    from connect_offers import sweep, RECONNECT_LINE
    u = _onboarded(db, existing_tools="fitbit")
    s = get_session(); s.add(Integration(user_id=u.id, provider="google_health", status="connected", meta={})); s.commit(); s.close()
    base.mark_revoked(u.id, "google_health")
    assert _integ(u.id, "google_health").meta.get("revoked_at")

    assert sweep(_now()) == 1
    b = _bodies(sms_capture)
    assert b[0] == RECONNECT_LINE["google_health"].format(device="fitbit") and "/c/google_health/" in b[1]
    assert _integ(u.id, "google_health").meta.get("reconnect_nudged_at")
    assert sweep(_now()) == 0 and len(sms_capture) == 2      # once per revoke

    # a later revoke (they reconnected, then the 7-day token died again) → nudge again
    base.complete_connection(u.id, "google_health", __import__("integrations.base", fromlist=["TokenBundle"]).TokenBundle(
        access_token="a", refresh_token="r", expires_at=_now() + timedelta(hours=1), scopes="s", external_id="x"))
    import time; time.sleep(0.01)
    base.mark_revoked(u.id, "google_health")
    assert sweep(_now()) == 1


def test_reconnect_nudge_respects_the_heartbeat_guardrails(db, sms_capture, monkeypatch):
    import heartbeat
    from models import get_session, Integration
    from integrations import base
    from connect_offers import sweep
    u = _onboarded(db)
    s = get_session(); s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={})); s.commit(); s.close()
    base.mark_revoked(u.id, "gcal")
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: "quiet_hours")
    assert sweep(_now()) == 0 and sms_capture == []
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: None)
    assert sweep(_now()) == 1


# ── 3. the first offers + the allowlist follow-through ───────────────────────

def test_first_offer_is_the_account_question_in_testing_mode(db, sms_capture):
    from connect_offers import sweep, OFFER_GCAL_ASK
    u = _onboarded(db)
    assert sweep(_now()) == 1
    assert _bodies(sms_capture) == [OFFER_GCAL_ASK]
    assert "gcal" in _u(u.id).connect_offers
    assert sweep(_now()) == 0, "one action per user per sweep; the next offer waits a day"


def test_first_offer_carries_the_link_when_allowlisted_or_published(db, sms_capture, monkeypatch):
    import config
    from connect_offers import sweep, OFFER_GCAL_LINK
    u = _onboarded(db, google_email="a@gmail.com", google_allowlisted_at=_now())
    assert sweep(_now()) == 1
    b = _bodies(sms_capture)
    assert b[0] == OFFER_GCAL_LINK and b[1].startswith("https://app.example/c/gcal/")
    assert _integ(u.id, "gcal").status == "pending"
    sms_capture.clear()
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    v = _onboarded(db)
    assert sweep(_now()) == 1 and _bodies(sms_capture)[0] == OFFER_GCAL_LINK


def test_offers_are_a_day_apart_and_follow_the_profile(db, sms_capture, monkeypatch):
    import config
    monkeypatch.setattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", True)   # wearable offers wait for Google's API approval
    from connect_offers import sweep, OFFER_GCAL_ASK, OFFER_BCOURSES, OFFER_HEALTH_ASK
    u = _onboarded(db, occupation="student", existing_tools="pixel watch, strava")
    t0 = _now()
    assert sweep(t0) == 1 and _bodies(sms_capture) == [OFFER_GCAL_ASK]
    assert sweep(t0 + timedelta(hours=23)) == 0
    assert sweep(t0 + timedelta(hours=25)) == 1 and _bodies(sms_capture)[-1] == OFFER_BCOURSES
    assert sweep(t0 + timedelta(hours=50)) == 1 and _bodies(sms_capture)[-1] == OFFER_HEALTH_ASK.format(device="pixel watch")
    assert sweep(t0 + timedelta(hours=75)) == 0, "every offer is once, ever"
    # no wearable named → no wearable offer; not a student → no bcourses offer
    sms_capture.clear()
    v = _onboarded(db, phone="+15550009001")
    assert sweep(t0) == 1 and sweep(t0 + timedelta(hours=25)) == 0


def test_too_new_or_already_connected_gets_no_offer(db, sms_capture):
    from models import get_session, Integration
    from connect_offers import sweep
    fresh = _onboarded(db, activated_at=_now() - timedelta(hours=3), created_at=_now() - timedelta(hours=3))
    assert sweep(_now()) == 0
    done = _onboarded(db, phone="+15550009002", google_email="a@gmail.com", google_allowlisted_at=_now())
    s = get_session(); s.add(Integration(user_id=done.id, provider="gcal", status="connected", meta={})); s.commit(); s.close()
    assert sweep(_now()) == 0 and sms_capture == []
    onboarding = _onboarded(db, phone="+15550009003", onboarding_step=2)
    pending = _onboarded(db, phone="+15550009004", waitlist_status="pending")
    assert sweep(_now()) == 0


def test_allowlisting_sends_the_promised_link(db, sms_capture, client):
    """They answered the account question → coach saved it → founder clicks "mark
    allowlisted" in the admin → the next sweep texts the link, once."""
    from connect_offers import sweep, OFFER_GCAL_ASK, ALLOWLISTED_LINE
    from agent_tools import handle_set_google_account
    u = _onboarded(db)
    sweep(_now())                                     # the account question
    handle_set_google_account(u.id, {"email": "jane@gmail.com"})
    assert sweep(_now() + timedelta(hours=30)) == 0, "not allowlisted yet — nothing to send"

    r = client.post(f"/admin/user/{u.id}/google-allowlisted")
    assert r.status_code == 200 and _u(u.id).google_allowlisted_at
    sms_capture.clear()
    assert sweep(_now() + timedelta(hours=30)) == 1
    b = _bodies(sms_capture)
    assert b[0] == ALLOWLISTED_LINE["gcal"] and "/c/gcal/" in b[1]
    assert sweep(_now() + timedelta(hours=31)) == 0 and "gcal_link" in _u(u.id).connect_offers


def test_admin_allowlist_route_can_set_the_account_too(db, client):
    u = _onboarded(db)
    r = client.post(f"/admin/user/{u.id}/google-allowlisted")
    assert r.status_code == 400
    r = client.post(f"/admin/user/{u.id}/google-allowlisted", json={"email": "Pat@Gmail.com"})
    assert r.status_code == 200
    row = _u(u.id)
    assert row.google_email == "pat@gmail.com" and row.google_allowlisted_at
    page = client.get(f"/admin/user/{u.id}").get_data(as_text=True)
    assert "pat@gmail.com" in page and "allowlisted" in page


def test_admin_page_marks_needs_allowlisting(db, client):
    u = _onboarded(db, google_email="x@gmail.com")
    page = client.get(f"/admin/user/{u.id}").get_data(as_text=True)
    assert "needs allowlisting" in page and "mark allowlisted" in page


def test_sweep_is_inert_with_flags_off(db, sms_capture, monkeypatch):
    import config
    from connect_offers import sweep
    _onboarded(db)
    monkeypatch.setattr(config, "CONNECT_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "RECONNECT_NUDGE_ENABLED", False)
    assert sweep(_now()) == 0 and sms_capture == []


def test_columns_are_migrated():
    from models import User
    assert hasattr(User, "google_email") and hasattr(User, "google_allowlisted_at") and hasattr(User, "connect_offers")
    import migrate
    src = open(migrate.__file__).read()
    for col in ("google_email", "google_allowlisted_at", "connect_offers"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in src


def test_integrations_block_states_absence_and_outranks_memory(monkeypatch):
    """Live 2026-10-02 (founder, mid demo): the coaching summary said 'Connected feeds:
    Google Calendar…' while the row had been removed; the block listed only what WAS
    connected, so the model said 'yeah i can see it' and argued when corrected."""
    import config
    from connect_offers import integrations_block
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    b = integrations_block(None, "bcourses connected · google_health connected", None)
    assert b.startswith("## INTEGRATIONS (code's list — the ONLY truth")
    assert "bcourses connected · google_health connected" in b
    assert "gcal: NOT connected (google calendar)" in b
    assert "canvas" not in b.lower().split("bcourses connected")[1].split("gcal")[0]   # no separate canvas line
    assert "say no, you can't, and send the connect link in that same turn" in b
    # nothing at all → every enabled provider is NOT connected
    b2 = integrations_block(None, None, None)
    assert "gcal: NOT connected" in b2 and "bcourses: NOT connected" in b2 and "google_health: NOT connected" in b2
    # connected rows (incl. multi-account) never get a NOT line
    b3 = integrations_block(None, "gcal [a@gmail.com] connected · gcal [b@gmail.com] connected", None)
    assert "gcal: NOT" not in b3
    # the google-account line rides along
    b4 = integrations_block(None, None, "google account: unknown — ask")
    assert b4.rstrip().endswith("don't ask \"want the link?\" first.") and "google account: unknown" in b4

"""Part 0 — shared integrations plumbing: crypto, connect token, row lifecycle,
refresh/revoke, the connect routes, and the send_connect_link tool.

No real provider is wired in Part 0, so the route/refresh tests register a fake
provider into base.PROVIDERS and tear it down."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from cryptography.fernet import Fernet

import config
from tests.factories import make_user


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    """Every test in this module gets a real Fernet key + a connect secret."""
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "INTEGRATIONS_BASE_URL", "https://app.example")
    yield


class FakeProvider:
    name = "faketest"
    label = "faketest"
    scopes = "read"

    def __init__(self, *, fail_exchange=False, fail_refresh=False, refresh_bundle=None):
        self.fail_exchange = fail_exchange
        self.fail_refresh = fail_refresh
        self.refresh_bundle = refresh_bundle

    def authorize_url(self, *, state, redirect_uri):
        return f"https://prov.example/auth?state={state}&redirect_uri={redirect_uri}"

    def exchange_code(self, code, *, redirect_uri):
        from integrations.base import TokenBundle
        if self.fail_exchange:
            raise RuntimeError("exchange boom")
        return TokenBundle(access_token="ACCESS", refresh_token="REFRESH",
                           expires_at=_utcnow() + timedelta(hours=6), scopes="read",
                           external_id="ext123")

    def refresh(self, refresh_token):
        if self.fail_refresh:
            raise RuntimeError("refresh boom")
        from integrations.base import TokenBundle
        return self.refresh_bundle or TokenBundle(
            access_token="ACCESS2", refresh_token="REFRESH2",
            expires_at=_utcnow() + timedelta(hours=6), scopes="read", external_id="ext123")

    def connected_message(self, integ):
        return "connected. i'll take it from here"

    def enabled(self):
        return True


@pytest.fixture
def fake_provider():
    from integrations import base
    prov = FakeProvider()
    base.register(prov)
    try:
        yield prov
    finally:
        base.PROVIDERS.pop("faketest", None)


# ─── crypto ───────────────────────────────────────────────────────────────────

def test_crypto_round_trip():
    from integrations import crypto
    ct = crypto.encrypt("sk-secret-token")
    assert ct != "sk-secret-token"          # actually encrypted
    assert crypto.decrypt(ct) == "sk-secret-token"
    assert crypto.encrypt(None) is None
    assert crypto.decrypt(None) is None


def test_crypto_refuses_without_key(monkeypatch):
    from integrations import crypto
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", "")
    with pytest.raises(crypto.MissingEncryptionKey):
        crypto.encrypt("x")


# ─── connect token ────────────────────────────────────────────────────────────

def test_connect_token_valid_expired_tampered():
    from integrations.tokens import connect_token, verify_connect_token
    token, nonce = connect_token(7, "gcal")
    assert verify_connect_token(token) == (7, "gcal", nonce)

    # expired
    expired, _ = connect_token(7, "gcal", exp=1)
    assert verify_connect_token(expired) is None

    # tampered mac
    assert verify_connect_token(token[:-2] + ("aa" if not token.endswith("aa") else "bb")) is None

    # malformed / bad provider
    assert verify_connect_token("garbage") is None
    assert verify_connect_token(None) is None


# ─── row lifecycle ────────────────────────────────────────────────────────────

def test_pending_then_complete_stores_ciphertext_and_burns_nonce(db):
    from integrations import base
    from integrations.tokens import connect_token
    from models import Integration
    user = make_user(db)

    token, nonce = connect_token(user.id, "faketest")
    base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)
    assert base.pending_nonce(user.id, "faketest") == nonce

    base.complete_connection(user.id, "faketest",
                             base.TokenBundle(access_token="ACCESS", refresh_token="REFRESH",
                                              expires_at=_utcnow() + timedelta(hours=6),
                                              scopes="read", external_id="ext123"))
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="faketest").one()
    assert row.status == "connected"
    assert row.access_token not in (None, "ACCESS")          # ciphertext, not plaintext
    assert "ACCESS" not in (row.access_token or "")
    assert row.external_id == "ext123"
    assert base.pending_nonce(user.id, "faketest") is None    # nonce burned


def test_status_line_never_leaks_token(db):
    from integrations import base
    user = make_user(db)
    base.complete_connection(user.id, "gcal", base.TokenBundle(access_token="ACCESS"))
    base.complete_connection(user.id, "strava",
                             base.TokenBundle(access_token="A", scopes="activity:read_all,activity:write"))
    line = base.status_line(user.id)
    assert "gcal connected" in line
    assert "strava connected (read+post)" in line
    assert "ACCESS" not in line and "A" not in line.replace("connected", "")


# ─── refresh / revoke ─────────────────────────────────────────────────────────

def test_get_valid_access_token_returns_when_fresh(db, fake_provider):
    from integrations import base
    user = make_user(db)
    base.complete_connection(user.id, "faketest",
                             base.TokenBundle(access_token="ACCESS", refresh_token="REFRESH",
                                              expires_at=_utcnow() + timedelta(hours=5)))
    assert base.get_valid_access_token(user.id, "faketest") == "ACCESS"


def test_get_valid_access_token_refreshes_near_expiry(db, fake_provider):
    from integrations import base
    from models import Integration
    user = make_user(db)
    base.complete_connection(user.id, "faketest",
                             base.TokenBundle(access_token="OLD", refresh_token="REFRESH",
                                              expires_at=_utcnow() + timedelta(seconds=30)))  # within skew
    tok = base.get_valid_access_token(user.id, "faketest")
    assert tok == "ACCESS2"
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="faketest").one()
    assert "OLD" not in (row.access_token or "") and "ACCESS2" not in (row.access_token or "")  # re-encrypted


def test_refresh_failure_revokes(db, monkeypatch):
    from integrations import base
    from models import Integration
    prov = FakeProvider(fail_refresh=True)
    base.register(prov)
    try:
        user = make_user(db)
        base.complete_connection(user.id, "faketest",
                                 base.TokenBundle(access_token="OLD", refresh_token="REFRESH",
                                                  expires_at=_utcnow() + timedelta(seconds=10)))
        assert base.get_valid_access_token(user.id, "faketest") is None
        db.expire_all()
        row = db.query(Integration).filter_by(user_id=user.id, provider="faketest").one()
        assert row.status == "revoked" and row.access_token is None
    finally:
        base.PROVIDERS.pop("faketest", None)


# ─── routes ───────────────────────────────────────────────────────────────────

def test_connect_start_unknown_provider_404(client):
    r = client.get("/c/nope?t=whatever")
    assert r.status_code == 404


def test_connect_start_redirects_to_authorize(client, db, fake_provider):
    from integrations import base
    from integrations.tokens import connect_token
    user = make_user(db)
    token, nonce = connect_token(user.id, "faketest")
    base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)

    r = client.get(f"/c/faketest?t={token}")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("https://prov.example/auth?")
    assert f"state={token}" in r.headers["Location"]
    assert "oauth/faketest/callback" in r.headers["Location"]


def test_connect_start_used_link_rejected(client, db, fake_provider):
    from integrations.tokens import connect_token
    user = make_user(db)
    token, nonce = connect_token(user.id, "faketest")  # never set_pending → no stored nonce
    r = client.get(f"/c/faketest?t={token}")
    assert r.status_code == 400


# ─── the short branded link (/c/<provider>/<code>) ───────────────────────────

def _handoff_target(resp) -> str:
    """The provider URL the handoff page sends the browser to (its JS redirect)."""
    import json as _json
    import re as _re
    m = _re.search(r"location\.replace\((\"[^<]*?\")\)", resp.get_data(as_text=True))
    assert m, "handoff page has no redirect"
    return _json.loads(m.group(1))


def _state_of(url: str) -> str:
    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(url).query)["state"][0]


def test_mint_connect_link_is_short_and_branded(db, fake_provider, monkeypatch):
    from integrations import base
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "https://app.cued.fit/")
    user = make_user(db)
    link = base.mint_connect_link(user.id, "faketest")
    assert link.startswith("https://app.cued.fit/c/faketest/")
    code = link.rsplit("/", 1)[1]
    assert len(code) == 12 and "?" not in link
    assert base.pending_nonce(user.id, "faketest") == code
    assert base.pending_by_code("faketest", code)[0] == user.id


def test_mint_connect_link_falls_back_to_the_flask_host(db, fake_provider, monkeypatch):
    from integrations import base
    monkeypatch.setattr(config, "CONNECT_LINK_BASE_URL", "")
    user = make_user(db)
    assert base.mint_connect_link(user.id, "faketest").startswith("https://app.example/c/faketest/")


def test_short_link_hands_off_with_a_valid_state_then_connects_once(client, db, fake_provider, sms_capture):
    from integrations import base
    from integrations.tokens import verify_connect_token
    user = make_user(db)
    code = base.mint_connect_link(user.id, "faketest").rsplit("/", 1)[1]

    r = client.get(f"/c/faketest/{code}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "og:title" in body and "connect your faketest · cued" in body
    target = _handoff_target(r)
    assert target.startswith("https://prov.example/auth?")
    assert "oauth/faketest/callback" in target   # the callback host is unchanged
    state = _state_of(target)
    assert verify_connect_token(state) == (user.id, "faketest", code)

    # the rebuilt token is accepted by the unchanged callback, and burns the code
    cb = client.get(f"/oauth/faketest/callback?code=abc&state={state}")
    assert cb.status_code == 200
    db.expire_all()
    assert base.get_integration(db, user.id, "faketest").status == "connected"
    # a re-tap of the link that WORKED says so (live 2026-10-06: two retries while the
    # first callback was still syncing both got "link already used")
    again = client.get(f"/c/faketest/{code}")
    assert again.status_code == 200 and "you're connected" in again.get_data(as_text=True)


def test_short_link_expired(client, db, fake_provider):
    from integrations import base
    user = make_user(db)
    code = base.mint_connect_link(user.id, "faketest").rsplit("/", 1)[1]
    base.set_pending(user.id, "faketest", code, 1)   # same code, exp long past
    r = client.get(f"/c/faketest/{code}")
    assert r.status_code == 400 and "expired" in r.get_data(as_text=True)


def test_short_link_replaced_by_a_newer_link_is_dead(client, db, fake_provider):
    from integrations import base
    user = make_user(db)
    old = base.mint_connect_link(user.id, "faketest").rsplit("/", 1)[1]
    new = base.mint_connect_link(user.id, "faketest").rsplit("/", 1)[1]
    assert client.get(f"/c/faketest/{old}").status_code == 400
    assert client.get(f"/c/faketest/{new}").status_code == 200


def test_short_link_unknown_or_malformed_code(client, db, fake_provider):
    assert client.get("/c/faketest/AAAAAAAAAAAA").status_code == 400
    assert client.get("/c/faketest/x").status_code == 400
    assert client.get("/c/nope/AAAAAAAAAAAA").status_code == 404


def test_short_code_is_scoped_to_its_provider(client, db, fake_provider):
    """A code minted for one provider never resolves under another provider's path."""
    from integrations import base
    user = make_user(db)
    code = base.mint_connect_link(user.id, "faketest").rsplit("/", 1)[1]
    assert base.pending_by_code("gcal", code) is None


def test_callback_happy_path_connects_and_texts(client, db, fake_provider, sms_capture):
    from integrations import base
    from integrations.tokens import connect_token
    from models import Integration
    user = make_user(db, phone="+15105551234")
    token, nonce = connect_token(user.id, "faketest")
    base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)

    r = client.get(f"/oauth/faketest/callback?code=abc&state={token}")
    assert r.status_code == 200
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="faketest").one()
    assert row.status == "connected"
    # confirmation text sent through the normal outbound path
    assert any("connected" in body for _phone, body in sms_capture)


def test_callback_is_single_use(client, db, fake_provider, sms_capture):
    from integrations import base
    from integrations.tokens import connect_token
    user = make_user(db, phone="+15105551234")
    token, nonce = connect_token(user.id, "faketest")
    base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)

    first = client.get(f"/oauth/faketest/callback?code=abc&state={token}")
    assert first.status_code == 200
    second = client.get(f"/oauth/faketest/callback?code=abc&state={token}")
    assert second.status_code == 200 and "you're connected" in second.get_data(as_text=True)   # burned, but connected
    # a dead link for a user who is NOT connected is still a 400
    other = make_user(db, phone="+15105551235")
    tok2, nonce2 = connect_token(other.id, "faketest")
    base.set_pending(other.id, "faketest", nonce2, int(_utcnow().timestamp()) + 1800)
    base.set_pending(other.id, "faketest", "replaced-nonce", int(_utcnow().timestamp()) + 1800)
    dead = client.get(f"/oauth/faketest/callback?code=abc&state={tok2}")
    assert dead.status_code == 400 and "already used" in dead.get_data(as_text=True)


def test_callback_bad_state_no_connection(client, db, fake_provider):
    from models import Integration
    user = make_user(db)
    r = client.get("/oauth/faketest/callback?code=abc&state=garbage")
    assert r.status_code == 400
    db.expire_all()
    assert db.query(Integration).filter_by(user_id=user.id).count() == 0


def test_callback_exchange_failure_marks_error_no_text(client, db, sms_capture):
    from integrations import base
    from integrations.tokens import connect_token
    from models import Integration
    prov = FakeProvider(fail_exchange=True)
    base.register(prov)
    try:
        user = make_user(db, phone="+15105551234")
        token, nonce = connect_token(user.id, "faketest")
        base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)
        r = client.get(f"/oauth/faketest/callback?code=abc&state={token}")
        assert r.status_code == 502
        db.expire_all()
        row = db.query(Integration).filter_by(user_id=user.id, provider="faketest").one()
        assert row.status == "error"
        assert "exchange" in (row.meta or {}).get("last_error", "")
        assert not sms_capture            # no text on failure
    finally:
        base.PROVIDERS.pop("faketest", None)


def test_callback_user_denied_is_neutral(client, db, fake_provider, sms_capture):
    user = make_user(db)
    r = client.get("/oauth/faketest/callback?error=access_denied&state=whatever")
    assert r.status_code == 200
    assert not sms_capture


# ─── send_connect_link tool ───────────────────────────────────────────────────

def test_tool_gated_off_returns_error(db, monkeypatch):
    from agent_tools import handle_send_connect_link
    monkeypatch.setattr(config, "GCAL_ENABLED", False)
    user = make_user(db)
    out = handle_send_connect_link(user.id, {"provider": "gcal"})
    assert out.startswith("error")


def test_tool_sends_link_bubble_and_sets_pending(db, monkeypatch, sms_capture):
    from agent_tools import handle_send_connect_link
    from integrations import base
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)   # the Testing-mode account gate is its own test (test_connect_offers)
    user = make_user(db, phone="+15105559999")
    out = handle_send_connect_link(user.id, {"provider": "gcal"})
    assert out.startswith("ok")
    # a link bubble went out, and it points at /c/gcal
    assert any("/c/gcal/" in body for _p, body in sms_capture)
    # pending nonce recorded so the callback can enforce single use
    assert base.pending_nonce(user.id, "gcal")



# ─── connect: say "connected" first, pull in the background (2026-10-06, user 48) ───

class _SyncingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.events = []

    def sync_now(self, user_id):
        self.events.append(("sync", user_id))


def test_callback_texts_and_returns_before_the_first_pull(client, db, sms_capture, monkeypatch):
    """The page and the confirmation text never wait on the first pull; the pull runs on a
    thread (inline here) AFTER the text. A pull that raises still leaves 'connected'."""
    import threading
    from integrations import base
    from integrations.tokens import connect_token
    from models import Integration
    prov = _SyncingProvider()
    base.register(prov)
    try:
        started = []
        monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None, name=None:
                            type("T", (), {"start": lambda self: (started.append(name), target(*args, **(kwargs or {})))})())
        user = make_user(db, phone="+15105551234")
        token, nonce = connect_token(user.id, "faketest")
        base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)
        import sms as _sms
        order = []
        real = _sms.send_sms
        def _spy(phone, body, **kw):
            order.append(("text", body))
            return real(phone, body, **kw)
        monkeypatch.setattr("sms.send_sms", _spy)
        r = client.get(f"/oauth/faketest/callback?code=abc&state={token}")
        assert r.status_code == 200 and "connected" in r.get_data(as_text=True)
        assert started == ["oauth-sync-faketest-%d" % user.id]
        assert prov.events == [("sync", user.id)]
        assert any("connected" in b for _p, b in sms_capture)
        db.expire_all()
        assert db.query(Integration).filter_by(user_id=user.id, provider="faketest").one().status == "connected"
    finally:
        base.PROVIDERS.pop("faketest", None)


def test_callback_flag_off_pulls_inline(client, db, sms_capture, monkeypatch):
    from integrations import base
    from integrations.tokens import connect_token
    monkeypatch.setattr(config, "OAUTH_SYNC_IN_BACKGROUND", False)
    prov = _SyncingProvider()
    base.register(prov)
    try:
        user = make_user(db, phone="+15105551234")
        token, nonce = connect_token(user.id, "faketest")
        base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)
        assert client.get(f"/oauth/faketest/callback?code=abc&state={token}").status_code == 200
        assert prov.events == [("sync", user.id)]
    finally:
        base.PROVIDERS.pop("faketest", None)


def test_long_link_re_tap_after_connect_says_connected(client, db, fake_provider, sms_capture):
    from integrations import base
    from integrations.tokens import connect_token
    user = make_user(db, phone="+15105551234")
    token, nonce = connect_token(user.id, "faketest")
    base.set_pending(user.id, "faketest", nonce, int(_utcnow().timestamp()) + 1800)
    assert client.get(f"/oauth/faketest/callback?code=abc&state={token}").status_code == 200
    r = client.get(f"/c/faketest?t={token}")
    assert r.status_code == 200 and "you're connected" in r.get_data(as_text=True)
    assert base.connected_by_code("faketest", nonce) == user.id and base.is_connected(user.id, "faketest")

"""
Multiple Google accounts per provider (2026-10-02). The primary row keeps account="" so
every existing caller is untouched; a second Google login for the same provider gets its
own row, keyed by that account's external_id. gcal_sync syncs every connected row with
its own token and sync state; a refresh failure revokes only that row; the status line
names each account; the reconnect nudge covers each row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GCAL_ENABLED", "RECONNECT_NUDGE_ENABLED", "GOOGLE_OAUTH_TESTING_MODE"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "INTEGRATIONS_BASE_URL", "https://app.example")
    import heartbeat
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: None)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bundle(ext, access="a", refresh="r"):
    from integrations.base import TokenBundle
    return TokenBundle(access_token=access, refresh_token=refresh, expires_at=_now() + timedelta(hours=1),
                       scopes="cal", external_id=ext)


def _rows(uid, provider="gcal"):
    from models import get_session
    from integrations.base import rows_for
    s = get_session()
    try:
        return [(r.account, r.external_id, r.status) for r in rows_for(s, uid, provider)]
    finally:
        s.close()


def test_first_connect_lands_on_the_primary_row(db):
    from integrations import base
    u = make_user(db)
    base.set_pending(u.id, "gcal", "n1", 10**10)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com"))
    assert _rows(u.id) == [("", "jane@gmail.com", "connected")]


def test_second_account_gets_its_own_row_and_the_primary_keeps_its_tokens(db):
    from integrations import base
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com", access="A1"))
    base.set_pending(u.id, "gcal", "n2", 10**10)                  # the coach sent another gcal link
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu", access="A2"))
    assert _rows(u.id) == [("", "jane@gmail.com", "connected"), ("jane@berkeley.edu", "jane@berkeley.edu", "connected")]
    assert base.get_valid_access_token(u.id, "gcal") == "A1"            # primary untouched
    assert base.pending_nonce(u.id, "gcal") is None                     # the handshake nonce was burned on the primary
    from models import get_session
    s = get_session()
    try:
        rows = base.rows_for(s, u.id, "gcal")
        assert base.get_valid_access_token(u.id, "gcal", integration_id=rows[1].id) == "A2"
    finally:
        s.close()


def test_reconnecting_a_known_account_updates_in_place(db):
    from integrations import base
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com", access="A1"))
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu", access="A2"))
    base.mark_revoked(u.id, "gcal")                                       # the primary's 7-day token died
    assert _rows(u.id)[0] == ("", "jane@gmail.com", "revoked")
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com", access="A3"))
    assert _rows(u.id) == [("", "jane@gmail.com", "connected"), ("jane@berkeley.edu", "jane@berkeley.edu", "connected")]
    assert base.get_valid_access_token(u.id, "gcal") == "A3"
    # and a secondary reconnect lands on the secondary row
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu", access="A4"))
    assert len(_rows(u.id)) == 2


def test_status_line_names_each_account_only_when_there_are_two(db):
    from integrations import base
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com"))
    assert base.status_line(u.id) == "gcal connected"
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu"))
    assert base.status_line(u.id) == "gcal [jane@gmail.com] connected · gcal [jane@berkeley.edu] connected"
    base.mark_revoked(u.id, "gcal")
    assert "gcal [jane@gmail.com] disconnected" in base.status_line(u.id)


def test_sync_covers_every_connected_row_with_its_own_state(db, monkeypatch):
    from integrations import base, gcal, gcal_sync
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com", access="TOK1"))
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu", access="TOK2"))
    seen = []
    monkeypatch.setattr(gcal, "list_calendars", lambda token: [{"id": f"cal-{token}", "summary": token}])

    def _events(token, cid, sync_token=None, time_min=None, time_max=None):
        seen.append((token, cid, sync_token))
        start = (_now() + timedelta(days=1)).replace(microsecond=0)
        return ([{"id": f"e-{token}", "summary": f"event {token}", "status": "confirmed",
                  "start": {"dateTime": start.isoformat() + "+00:00"},
                  "end": {"dateTime": (start + timedelta(hours=1)).isoformat() + "+00:00"}}], f"next-{token}")
    monkeypatch.setattr(gcal, "list_events", _events)

    r = gcal_sync.sync_user(u.id)
    assert r == {"upserted": 2, "deleted": 0, "accounts": 2}
    assert [t for t, _c, _s in seen] == ["TOK1", "TOK2"]
    from models import get_session, Event
    s = get_session()
    try:
        titles = sorted(e.title for e in s.query(Event).filter(Event.user_id == u.id, Event.source == "gcal"))
        rows = base.rows_for(s, u.id, "gcal")
        states = [(r.meta or {}).get("gcal_sync") for r in rows]
    finally:
        s.close()
    assert titles == ["event TOK1", "event TOK2"]
    assert states == [{"cal-TOK1": "next-TOK1"}, {"cal-TOK2": "next-TOK2"}]      # per-row sync tokens
    # second pass sends each row's own sync token
    seen.clear(); gcal_sync.sync_user(u.id)
    assert [s_ for _t, _c, s_ in seen] == ["next-TOK1", "next-TOK2"]


def test_refresh_failure_revokes_only_that_row(db, monkeypatch):
    from integrations import base
    from models import get_session
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com", access="A1"))
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu", access="A2"))
    s = get_session()
    try:
        rows = base.rows_for(s, u.id, "gcal")
        rows[1].expires_at = _now() - timedelta(minutes=1)      # the secondary needs a refresh
        s.commit()
        sid = rows[1].id
    finally:
        s.close()
    prov = base.get_provider("gcal")
    monkeypatch.setattr(type(prov), "refresh", lambda self, rt: (_ for _ in ()).throw(RuntimeError("400 invalid_grant")))
    assert base.get_valid_access_token(u.id, "gcal", integration_id=sid) is None
    assert _rows(u.id) == [("", "jane@gmail.com", "connected"), ("jane@berkeley.edu", "jane@berkeley.edu", "revoked")]
    assert base.get_valid_access_token(u.id, "gcal") == "A1"


def test_reconnect_nudge_names_the_dead_secondary_account(db, sms_capture):
    from integrations import base
    from connect_offers import sweep
    from models import get_session
    u = make_user(db, activated_at=_now() - timedelta(days=3))
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com"))
    base.complete_connection(u.id, "gcal", _bundle("jane@berkeley.edu"))
    s = get_session()
    try:
        sid = base.rows_for(s, u.id, "gcal")[1].id
    finally:
        s.close()
    base.mark_revoked(u.id, "gcal", integration_id=sid)
    assert sweep(_now()) == 1
    bodies = [b for _p, b in sms_capture]
    assert bodies[0] == "ur google calendar (jane@berkeley.edu) disconnected on google's end. tap to reconnect"
    assert "/c/gcal/" in bodies[1]
    assert sweep(_now()) == 0                                            # once per revoke


def test_a_second_account_needs_its_own_allowlisting_in_testing_mode(db):
    from integrations import base
    from connect_offers import allowlist_state, set_google_account
    u = make_user(db)
    base.complete_connection(u.id, "gcal", _bundle("jane@gmail.com"))
    assert allowlist_state(u) == "ok"                                    # connected before → on the list
    set_google_account(u.id, "jane@berkeley.edu")                        # "my school calendar is on another login"
    from models import get_session, User
    s = get_session()
    try:
        row = s.get(User, u.id)
        assert allowlist_state(row, s) == "needs_allowlist"
    finally:
        s.close()


def test_model_and_migration():
    from models import Integration
    assert hasattr(Integration, "account")
    names = {c.name for c in Integration.__table__.constraints if hasattr(c, "columns")}
    assert "uq_integrations_user_provider_account" in names
    import migrate
    src = open(migrate.__file__).read()
    assert "ADD COLUMN IF NOT EXISTS account VARCHAR(64) NOT NULL DEFAULT ''" in src
    assert "DROP CONSTRAINT IF EXISTS uq_integrations_user_provider" in src
    assert "DROP CONSTRAINT IF EXISTS integrations_user_id_provider_key" in src   # prod's actual key name
    assert "uq_integrations_user_provider_account" in src

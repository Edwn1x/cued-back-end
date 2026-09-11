"""
Hardening from the 2026-09-11 deploy incident: an app connection "idle in
transaction" (a model call with no timeout, inside an open DB session) held a
users lock; migrate.py's ALTER TABLE queued behind it for 14 minutes; every users
read queued behind the ALTER. Three fixes, each pinned here:
  1. every Anthropic client is bounded (llm_client.make_client) — and nothing
     constructs one anywhere else;
  2. the message processor releases its DB session BEFORE the model turn;
  3. migrate.py never requests a lock for an already-applied statement, sets a
     lock_timeout, retries, and fails the boot loudly instead of queueing the world.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

import pytest

from tests.factories import make_user

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 1. bounded clients, one factory ──────────────────────────────────────────

def test_make_client_is_bounded_by_config(monkeypatch):
    import config
    from llm_client import make_client
    monkeypatch.setattr(config, "ANTHROPIC_TIMEOUT_S", 77.0)
    monkeypatch.setattr(config, "ANTHROPIC_MAX_RETRIES", 1)
    c = make_client()
    assert float(c.timeout) == 77.0 if not hasattr(c.timeout, "read") else c.timeout.read == 77.0
    assert c.max_retries == 1
    assert 0 < config.ANTHROPIC_TIMEOUT_S <= 300, "a hung call must be bounded well under the SDK's 10-minute default"


def test_no_module_constructs_an_anthropic_client_directly():
    out = subprocess.run(["grep", "-rln", "--include=*.py", r"Anthropic(api_key", REPO,
                          "--exclude-dir=tests", "--exclude-dir=node_modules", "--exclude-dir=rewrite"],
                         capture_output=True, text=True).stdout.split()
    offenders = [os.path.relpath(f, REPO) for f in out if not f.endswith("llm_client.py")]
    assert offenders == [], f"construct clients via llm_client.make_client only: {offenders}"


# ── 2. the message processor releases its session before the model turn ──────

def test_process_buffered_message_closes_its_session_before_the_model_turn(db, anthropic_stub, sms_capture, monkeypatch):
    import app, models
    opened: list = []

    class _Tracked:
        def __init__(self, real):
            self._real, self.closed = real, False
        def close(self):
            self.closed = True
            return self._real.close()
        def __getattr__(self, name):
            return getattr(self._real, name)

    real_get = models.get_session
    def _tracked():
        t = _Tracked(real_get()); opened.append(t); return t
    monkeypatch.setattr(app, "get_session", _tracked)

    seen = {}
    def _loop(user, body, mtype, image_data=None):
        seen["outer_closed_at_model_time"] = opened[0].closed
        return "hey"
    monkeypatch.setattr(app, "run_agent_loop", _loop)
    monkeypatch.setattr(app.config, "SINGLE_AGENT_LOOP_ENABLED", True)
    user = make_user(db, onboarding_step=3)
    app.process_buffered_message(user.id, "hi", "freeform")
    assert seen["outer_closed_at_model_time"] is True, "the model turn ran inside the processor's open DB transaction"
    assert sms_capture, "the reply still went out"


# ── 3. migrate.py lock discipline ────────────────────────────────────────────

def test_already_applied_skips_existing_columns_and_wide_enough_types(db):
    import migrate, models
    with models.engine.connect() as conn:
        assert migrate.already_applied(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(200)")
        assert migrate.already_applied(conn, "ALTER TABLE users ADD COLUMN email VARCHAR(200)")  # legacy form too
        assert migrate.already_applied(conn, "ALTER TABLE users ADD COLUMN definitely_not_a_column TEXT") is None
        assert migrate.already_applied(conn, "ALTER TABLE users ALTER COLUMN activity_level TYPE VARCHAR(500)")
        assert migrate.already_applied(conn, "ALTER TABLE users ALTER COLUMN activity_level TYPE VARCHAR(9000)") is None
        assert migrate.already_applied(conn, "CREATE TABLE IF NOT EXISTS events (id SERIAL PRIMARY KEY)") is None


def test_run_migrations_on_a_current_schema_requests_no_alter_locks(db, caplog, monkeypatch):
    """The test DB is created from models.py, so every ADD COLUMN / widening is already
    applied: the runner must SKIP them via the pre-check — zero ALTER statements executed."""
    import migrate
    executed: list = []
    real_connect = migrate.engine.connect

    class _Conn:
        def __init__(self, real):
            self._r = real
        def execute(self, stmt, *a, **k):
            executed.append(str(stmt))
            return self._r.execute(stmt, *a, **k)
        def __getattr__(self, n):
            return getattr(self._r, n)
        def __enter__(self):
            self._r.__enter__(); return self
        def __exit__(self, *a):
            return self._r.__exit__(*a)
    monkeypatch.setattr(migrate.engine, "connect", lambda: _Conn(real_connect()))

    with caplog.at_level(logging.INFO):
        migrate.run_migrations()

    alters = [s for s in executed if re.match(r"\s*ALTER TABLE", s, re.I)]
    assert alters == [], f"ALTERs executed on an up-to-date schema (each takes an ACCESS EXCLUSIVE lock): {alters[:3]}"
    assert any("SET lock_timeout" in s for s in executed)
    assert any("no lock" in r.getMessage() for r in caplog.records)


def test_lock_timeout_is_retried_then_fails_the_boot(monkeypatch, caplog):
    import migrate

    class _LockErr(Exception):
        pass

    class _Conn:
        def __init__(self): self.calls = []
        def execute(self, stmt, *a, **k):
            s = str(stmt); self.calls.append(s)
            if "information_schema" in s:
                class _R:
                    def first(self_inner): return None
                return _R()
            if "ALTER TABLE" in s:
                raise _LockErr("canceling statement due to lock timeout")
            return None
        def commit(self): pass
        def rollback(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    conn = _Conn()
    monkeypatch.setattr(migrate, "MIGRATIONS", ["ALTER TABLE users ADD COLUMN IF NOT EXISTS zzz TEXT"])
    monkeypatch.setattr(migrate.engine, "connect", lambda: conn)
    slept: list = []
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="1 migration statement"):
            migrate.run_migrations(sleep=lambda s: slept.append(s))
    alters = [c for c in conn.calls if "ALTER TABLE" in c]
    assert len(alters) == migrate.LOCK_RETRIES
    assert len(slept) == migrate.LOCK_RETRIES - 1
    assert any("LOCK_TIMEOUT attempt" in r.getMessage() and "idle in" in r.getMessage() for r in caplog.records)

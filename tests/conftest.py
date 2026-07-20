"""
Tier-1 harness wiring.

Order that matters: env vars (dummy keys + the disposable-Postgres DATABASE_URL)
must be set BEFORE any project module is imported, because config.py reads them
at import and models.py binds its engine at import. So the cluster comes up in
pytest_configure, before test collection imports app.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the repo root importable (conftest lives in tests/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests._pgcluster import PgCluster  # stdlib-only, safe to import early
from tests import _sync
from tests._fake_anthropic import FakeAnthropicController, make_create

_CLUSTER: PgCluster | None = None


# ─── session / collection hooks ──────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--run-tier2", action="store_true", default=False,
        help="Run tier-2 (live-model, metered) tests too.",
    )


def pytest_configure(config):
    global _CLUSTER
    # dummy secrets so import-time client constructors (Anthropic, Twilio) succeed
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-tier1")
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACdummy")
    os.environ.setdefault("TWILIO_AUTH_TOKEN", "dummytoken")
    os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
    os.environ.setdefault("USER_PROFILE_MEMORY_ENABLED", "true")

    # CI provides a Postgres 18 service container via CUED_TEST_DATABASE_URL
    # (GitHub runners don't ship the PG18 server binaries). Locally we spin up a
    # disposable native cluster. Either way DATABASE_URL is a throwaway test DB,
    # never prod.
    external = os.environ.get("CUED_TEST_DATABASE_URL")
    if external:
        os.environ["DATABASE_URL"] = external
    else:
        _CLUSTER = PgCluster()
        _CLUSTER.start()
        os.environ["DATABASE_URL"] = _CLUSTER.dsn()


def pytest_unconfigure(config):
    global _CLUSTER
    if _CLUSTER is not None:
        _CLUSTER.stop()
        _CLUSTER = None


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-tier2"):
        return
    skip = pytest.mark.skip(reason="tier-2 (live model); pass --run-tier2 to run")
    for item in items:
        if "tier2" in item.keywords:
            item.add_marker(skip)


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _appmod():
    """Import the app once the cluster is up; disable the rate limiter for tests."""
    import app  # runs init_db() against the test cluster
    import models
    app.app.config["RATELIMIT_ENABLED"] = False
    app.app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _truncate(_appmod):
    """Reset every table (and identity sequences) before each test."""
    import models
    from sqlalchemy import text
    tables = ", ".join(t.name for t in reversed(models.Base.metadata.sorted_tables))
    with models.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    """A session for test setup and assertions. Call db.expire_all() before a
    read that follows app-side commits, or use a fresh get_session()."""
    import models
    s = models.get_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def sms_capture(_appmod, monkeypatch):
    """Capture outbound SMS instead of hitting Twilio; kill inter-part sleeps.

    Patching sms._send_single centrally catches every caller (app + scheduler),
    while send_sms still runs its real splitting + outbound-Message logging."""
    import sms
    captured: list = []

    def _fake_send_single(phone, body):
        captured.append((phone, body))
        return f"SMfake{len(captured):028d}"

    monkeypatch.setattr(sms, "_send_single", _fake_send_single)
    monkeypatch.setattr(sms.time, "sleep", lambda *_a, **_k: None)
    return captured


@pytest.fixture(autouse=True)
def sync_exec(_appmod, monkeypatch):
    """Make post-reply daemon threads synchronous and the buffer timer deterministic."""
    import app
    import message_buffer
    _sync.clear_pending()
    monkeypatch.setattr(app, "threading", _sync.make_threading_shim(Thread=_sync.SyncThread))
    monkeypatch.setattr(message_buffer, "threading",
                        _sync.make_threading_shim(Timer=_sync.FakeTimer))
    yield
    _sync.clear_pending()


@pytest.fixture(autouse=True)
def anthropic_stub(_appmod, request, monkeypatch):
    """Patch anthropic Messages.create centrally. Disabled for tier-2 (live)."""
    if "tier2" in request.keywords:
        yield None
        return
    import anthropic
    controller = FakeAnthropicController()
    try:
        target = anthropic.resources.messages.Messages
    except AttributeError:  # older/newer layout fallback
        target = anthropic.resources.Messages
    monkeypatch.setattr(target, "create", make_create(controller))
    yield controller


@pytest.fixture
def client(_appmod):
    return _appmod.app.test_client()


@pytest.fixture
def driver(client, sms_capture):
    from tests.driver import Driver
    return Driver(client, sms_capture)

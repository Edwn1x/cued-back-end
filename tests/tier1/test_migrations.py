"""
Phase 1 — migrations are tested (roadmap rule).

Runs the real migrate.py MIGRATIONS against the test DB and asserts idempotency +
that the new tables exist, then checks that a realistic CURRENT-shape profile
(legacy entries with no validity fields) survives and renders — proving the
absence-means-valid design needs no backfill.

NOTE: a scrubbed export of the founder's own user row is the one organically-grown
data shape that exists; it slots in here as an additional fixture when provided
(parked input, like the Phase 3 screenshots). The synthetic legacy fixture below
covers the JSON shape in the meantime.
"""

from __future__ import annotations


def test_migrations_are_idempotent_and_create_new_tables(db):
    from sqlalchemy import inspect
    import models
    from migrate import run_migrations

    # Idempotent: safe to run twice against an already-migrated DB.
    run_migrations()
    run_migrations()

    insp = inspect(models.engine)
    tables = set(insp.get_table_names())
    assert {"events", "processed_messages", "heartbeat_ticks",
            "consolidation_runs", "episodic_digests"} <= tables

    user_cols = {c["name"] for c in insp.get_columns("users")}
    assert "last_episodic_message_id" in user_cols

    cr_cols = {c["name"] for c in insp.get_columns("consolidation_runs")}
    assert {"user_id", "aborted", "summary", "diff", "prev_profile", "removed_count"} <= cr_cols

    ep_cols = {c["name"] for c in insp.get_columns("episodic_digests")}
    assert {"user_id", "occurred_on", "text", "deleted_at"} <= ep_cols

    event_cols = {c["name"] for c in insp.get_columns("events")}
    assert {"user_id", "event_type", "occurred_at", "ends_at", "source", "raw_text"} <= event_cols

    # Burn-in: manage_log edit audit column on all three editable tables.
    for tbl in ("meals", "workouts", "events"):
        assert "edits" in {c["name"] for c in insp.get_columns(tbl)}, f"{tbl}.edits missing"

    pm_cols = {c["name"] for c in insp.get_columns("processed_messages")}
    assert {"message_sid", "user_id", "received_at"} <= pm_cols

    hb_cols = {c["name"] for c in insp.get_columns("heartbeat_ticks")}
    assert {"user_id", "decided_at", "spoke", "reason", "message",
            # addendum: search-budget instrumentation (the search decision, not just outcome)
            "search_available", "search_used", "search_query"} <= hb_cols


def test_run_migrations_swallows_already_exists(db, monkeypatch):
    """Deploy hardening (post-burn-in): migrate.py now runs at boot (Procfile), and a
    genuine failure must block boot — but idempotency (re-running an already-applied
    ALTER/CREATE) must still be silently OK, or every normal boot would fail."""
    import migrate

    from migrate import run_migrations as _apply_baseline
    _apply_baseline()  # ensure the columns/tables below already exist on this DB

    monkeypatch.setattr(migrate, "MIGRATIONS", [
        # deliberately WITHOUT "IF NOT EXISTS" / "already exists" guards, so these
        # only pass if the swallow branch (not the raise branch) catches them.
        "ALTER TABLE users ADD COLUMN user_timezone VARCHAR(50)",
        "CREATE TABLE events (id SERIAL PRIMARY KEY)",
    ])
    migrate.run_migrations()  # must not raise


def test_run_migrations_raises_on_genuine_failure(db, monkeypatch):
    """A real failure (not 'already exists') must raise so the Procfile's `&&` chain
    blocks the app from booting against a half-migrated schema."""
    import migrate

    monkeypatch.setattr(migrate, "MIGRATIONS", [
        "ALTER TABLE this_table_does_not_exist ADD COLUMN x INTEGER",
    ])
    try:
        migrate.run_migrations()
        assert False, "expected run_migrations() to raise on a genuine failure"
    except RuntimeError as e:
        assert "this_table_does_not_exist" in str(e) or "failed" in str(e).lower()


def test_legacy_profile_survives_and_renders_as_valid(db):
    """A pre-validity-windows profile (entries with no invalidated_* fields, no
    __history__ bucket) is fully valid and renders — no backfill required."""
    from tests.factories import make_user
    from memory import build_memory_block

    legacy = {
        "constraints": [
            {"id": "c1", "text": "severe peanut allergy",
             "ts": "2025-01-02T10:00:00+00:00", "uses": 5, "safety": True},
        ],
        "goals": [
            {"id": "g1", "text": "cut to 12% body fat by june",
             "ts": "2025-01-02T10:00:00+00:00", "uses": 2},
        ],
        "schedule": [
            {"id": "s1", "text": "trains 4 days per week",
             "ts": "2025-01-02T10:00:00+00:00", "uses": 1},
        ],
    }
    user = make_user(db, user_profile_memory=legacy)

    assert "peanut allergy" in build_memory_block(user, "nutrition")  # safety = universal
    assert "cut to 12%" in build_memory_block(user, "coach")          # goals in coach slice
    assert "trains 4 days" in build_memory_block(user, "training")    # schedule in training slice

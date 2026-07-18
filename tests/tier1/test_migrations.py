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
    assert {"events", "processed_messages"} <= tables

    event_cols = {c["name"] for c in insp.get_columns("events")}
    assert {"user_id", "event_type", "occurred_at", "ends_at", "source", "raw_text"} <= event_cols

    pm_cols = {c["name"] for c in insp.get_columns("processed_messages")}
    assert {"message_sid", "user_id", "received_at"} <= pm_cols


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

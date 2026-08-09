"""
Background-job truncation (rewrite/vision-thoroughness Fix 2).

Live bug (Aug 7-8, visible once universal stop= logging shipped):
extract_and_store_decisions and maybe_update_coaching_summary hit stop=max_tokens —
the fourth surface of the output-ceiling class. These jobs write DURABLE state
(profile fields, memory facts, the rolling summary + its watermark), so a truncated
output stored is silent data corruption; the summary job even advances the watermark
over messages it only partially folded. These pin the fix: raised per-site ceilings
sized for worst-case output, and a stop_reason gate BEFORE anything is parsed or
stored — the danger case is a truncated response whose text still parses.
"""

from __future__ import annotations

import logging

from tests.factories import make_user


def _seed_messages(db, user, n):
    from models import Message
    for i in range(n):
        db.add(Message(user_id=user.id, direction="in" if i % 2 else "out",
                       body=f"message {i} about training and food"))
    db.commit()


def _refetch(db, user_id):
    from models import User
    db.expire_all()
    return db.get(User, user_id)


def _truncation_logs(caplog):
    return [r.getMessage() for r in caplog.records if "BG_JOB_TRUNCATED" in r.getMessage()]


# ---- ceilings sized for the output actually asked for (revert guard) ---------

def test_decisions_ceiling_fits_full_field_set(db, anthropic_stub):
    """14 populated JSON fields + fences + a free-text food_context is ~300-400
    tokens; the old 250 cap truncated live (Aug 7-8 stop= lines)."""
    from app import extract_and_store_decisions

    user = make_user(db)
    extract_and_store_decisions(user.id, "I'm 20, cutting at 2200", "on it")

    assert anthropic_stub.calls, "job must reach the model"
    assert anthropic_stub.calls[-1]["max_tokens"] >= 1000


def test_memory_extract_ceiling_fits_multi_fact_turn(db, anthropic_stub):
    """A dense turn legitimately emits 6-8 facts (~600+ tokens with fences); the
    600 cap is inside that range."""
    from app import extract_and_store_memory

    user = make_user(db)
    extract_and_store_memory(user.id, "new gym, new goal, Tuesdays are packed", "noted")

    assert anthropic_stub.calls, "job must reach the model"
    assert anthropic_stub.calls[-1]["max_tokens"] >= 1500


def test_summary_ceiling_exceeds_its_own_asked_for_length(db, anthropic_stub):
    """The prompt asks for up to 400 words of structured summary — ~550-700 tokens
    WITH headers, i.e. the old 600 cap sat INSIDE the asked-for output range."""
    from app import maybe_update_coaching_summary

    user = make_user(db)
    _seed_messages(db, user, 20)
    maybe_update_coaching_summary(user.id)

    assert anthropic_stub.calls, "summary job must reach the model"
    assert anthropic_stub.calls[-1]["max_tokens"] >= 1500


# ---- truncated output is discarded, never stored (even when it parses) -------

def test_truncated_decisions_never_store_even_if_parseable(db, anthropic_stub, caplog):
    """stop=max_tokens with text that happens to be valid JSON: you can't know
    what was cut, so nothing is trusted. Today this stores (the parse-failure
    except path is the only accidental guard)."""
    from app import extract_and_store_decisions
    from tests._fake_anthropic import Truncated

    user = make_user(db)
    assert user.age is None
    anthropic_stub.reply_with(lambda kw: Truncated('{"age": 30, "goal_priority": "cutting"}'))

    with caplog.at_level(logging.WARNING):
        extract_and_store_decisions(user.id, "I'm 30, let's cut", "on it")

    user = _refetch(db, user.id)
    assert user.age is None, "truncated extraction must not write profile fields"
    assert user.confirmed_goal_priority is None
    assert _truncation_logs(caplog), "truncation must announce itself by name"


def test_truncated_memory_extract_never_stores(db, anthropic_stub, caplog):
    from app import extract_and_store_memory
    from tests._fake_anthropic import Truncated

    user = make_user(db)
    before = dict(user.user_profile_memory or {})
    payload = ('{"facts": [{"action": "add", "category": "goals", '
               '"text": "wants visible abs", "replaces_text": null, '
               '"safety_critical": false}]}')
    anthropic_stub.reply_with(lambda kw: Truncated(payload))

    with caplog.at_level(logging.WARNING):
        extract_and_store_memory(user.id, "want abs by summer", "let's go")

    user = _refetch(db, user.id)
    after = dict(user.user_profile_memory or {})
    assert after == before, "truncated fact list must not reach apply_facts"
    assert _truncation_logs(caplog)


def test_truncated_summary_keeps_prior_and_watermark(db, anthropic_stub, caplog):
    """The worst of the three: a partial summary REPLACES the intact prior one and
    the watermark advances past messages that were only partially folded — the raw
    history is then permanently behind a corrupted summary. A truncated run must
    leave both untouched so the next cycle refolds the same cohort."""
    from app import maybe_update_coaching_summary
    from tests._fake_anthropic import Truncated

    user = make_user(db, coaching_summary="intact prior summary")
    _seed_messages(db, user, 20)
    anthropic_stub.reply_with(
        lambda kw: Truncated("## Coaching Decisions\n- set calories at 22"))

    with caplog.at_level(logging.WARNING):
        maybe_update_coaching_summary(user.id)

    user = _refetch(db, user.id)
    assert user.coaching_summary == "intact prior summary", \
        "a partial summary is worse than last cycle's intact one"
    assert user.last_compressed_message_id is None, \
        "watermark must not advance over messages that were never fully folded"
    assert _truncation_logs(caplog)


# ---- normal completions still store (regression) ------------------------------

def test_clean_decisions_still_store(db, anthropic_stub):
    from app import extract_and_store_decisions

    user = make_user(db)
    anthropic_stub.reply_with(lambda kw: '{"age": 30, "goal_priority": "cutting"}')
    extract_and_store_decisions(user.id, "I'm 30, let's cut", "on it")

    user = _refetch(db, user.id)
    assert user.age == 30
    assert user.confirmed_goal_priority == "cutting"


def test_clean_memory_extract_still_stores(db, anthropic_stub):
    from app import extract_and_store_memory

    user = make_user(db)
    payload = ('{"facts": [{"action": "add", "category": "goals", '
               '"text": "wants visible abs", "replaces_text": null, '
               '"safety_critical": false}]}')
    anthropic_stub.reply_with(lambda kw: payload)
    extract_and_store_memory(user.id, "want abs by summer", "let's go")

    user = _refetch(db, user.id)
    profile = user.user_profile_memory or {}
    assert any("abs" in e.get("text", "") for e in profile.get("goals", [])), \
        f"clean extraction regressed: {profile!r}"


def test_clean_summary_still_stores_and_advances(db, anthropic_stub):
    from app import maybe_update_coaching_summary

    user = make_user(db, coaching_summary="intact prior summary")
    _seed_messages(db, user, 20)
    anthropic_stub.reply_with(lambda kw: "## Coaching Decisions\n- set calories at 2200")
    maybe_update_coaching_summary(user.id)

    user = _refetch(db, user.id)
    assert user.coaching_summary and "2200" in user.coaching_summary
    assert user.last_compressed_message_id, "watermark must advance on a clean fold"

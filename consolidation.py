"""
Phase 5 — nightly consolidation. A batch job that keeps the memory profile clean:
closes stale/never-used facts, merges near-duplicates, collapses contradictions
toward recency. It is the first writer to memory NOT triggered by a user turn, so
the whole design is guardrails against silent cross-night drift:

  - copy-on-write: the candidate is a deepcopy; live memory is never mutated
    mid-computation, and is only written if the run passes every check.
  - active safety entries are UNTOUCHED (never closed/merged/deduped) — with ONE
    audited exception: supersede_resolved_safety may close a stale safety state
    that a newer same-topic resolution entry supersedes ("fully recovered" closes
    "currently experiencing…"), each closure trigger-recorded and WARNING-logged.
    The dropped-safety invariant accepts exactly those ids and nothing else.
  - bounded delta: a run that would remove more than a configured fraction of valid
    entries ABORTS — nothing is written, an alert is logged.
  - full JSON diff + a one-line HUMAN-READABLE per-user summary + the pre-run
    profile snapshot (rollback) are persisted on every run that changes anything.

The structural passes are pure Python over the candidate (deterministic, tier-1
tested); they reuse memory.py's existing dedup/supersession primitives. The
coaching-summary refresh is a separate optional model step (tier-2 graded), kept
out of the hard-invariant core.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

import config
from models import get_session, User, ConsolidationRun
from memory import (
    HISTORY_KEY, CATEGORIES, invalidate_entry,
    expire_stale_entries, supersede_resolved_safety,
    _find_duplicate, _find_supersession_target, _tokenize, _jaccard,
)

logger = logging.getLogger("cued.consolidation")


def _valid_entries(profile: dict):
    """All live (non-history) entries as (category, entry) pairs."""
    for cat, entries in (profile or {}).items():
        if cat == HISTORY_KEY:
            continue
        for e in (entries or []):
            yield cat, e


def _valid_count(profile: dict) -> int:
    return sum(1 for _ in _valid_entries(profile))


def _age_days(entry: dict, now: datetime) -> float:
    ts = entry.get("ts")
    if not ts:
        return 0.0
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0
    return (now - parsed).total_seconds() / 86400.0


def _short(text: str, n: int = 40) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ─── structural passes (pure; mutate the candidate, record human-readable ops) ──

def _expire_ttl(candidate: dict, ops: list, now: datetime) -> None:
    """TTL-aged categories (food_on_hand): close entries past their shelf life —
    covers write-quiet users the apply_facts sweep never reaches."""
    for cat, e in expire_stale_entries(candidate, now=now):
        ops.append(("closed", f"{_short(e.get('text'))}, ttl-expired"))


def _supersede_safety(candidate: dict, ops: list, allowed_safety_closures: set) -> None:
    """Superseded safety states close via the trigger-audited mechanism; the ids
    closed here are the ONLY safety removals the invariant below will accept."""
    for cat, e, resolver in supersede_resolved_safety(candidate):
        allowed_safety_closures.add(e["id"])
        ops.append(("superseded",
                    f"{_short(e.get('text'))} (safety, resolved by: {_short(resolver.get('text'))})"))


def _close_stale(candidate: dict, ops: list, now: datetime) -> None:
    """Close (invalidate) non-safety entries that are old AND never referenced."""
    limit = config.CONSOLIDATION_STALE_DAYS
    for cat, e in list(_valid_entries(candidate)):
        if e.get("safety"):
            continue
        if e.get("uses", 0) == 0 and _age_days(e, now) > limit:
            if invalidate_entry(candidate, e["id"], by="consolidation:stale"):
                ops.append(("closed", f"{_short(e.get('text'))}, stale {int(_age_days(e, now))}d"))


def _merge_near_dupes(candidate: dict, ops: list) -> None:
    """Within each category, collapse near-duplicate facts to the strongest entry
    (more uses; tie -> newer). Safety entries are never a merge target or loser."""
    for cat in CATEGORIES:
        entries = [e for e in (candidate.get(cat) or []) if not e.get("safety")]
        merged_ids = set()
        merged = 0
        for i, e in enumerate(entries):
            if e["id"] in merged_ids:
                continue
            # find later near-dupes of e
            dupes = []
            for other in entries[i + 1:]:
                if other["id"] in merged_ids:
                    continue
                if _find_duplicate([e], other.get("text", "")):
                    dupes.append(other)
            if not dupes:
                continue
            group = [e] + dupes
            keeper = max(group, key=lambda x: (x.get("uses", 0), x.get("ts", "")))
            for loser in group:
                if loser["id"] == keeper["id"]:
                    continue
                if invalidate_entry(candidate, loser["id"], by="consolidation:merge"):
                    merged_ids.add(loser["id"])
                    merged += 1
        if merged:
            label = cat.replace("_", " ")
            ops.append(("merged", f"{merged} {label} near-dupe{'s' if merged != 1 else ''}"))


def _collapse_contradictions(candidate: dict, ops: list) -> None:
    """Same fact, divergent number -> keep the newest, close the stale value."""
    for cat in CATEGORIES:
        # iterate newest-first so a newer entry supersedes older ones
        entries = sorted(
            [e for e in (candidate.get(cat) or []) if not e.get("safety")],
            key=lambda x: x.get("ts", ""), reverse=True,
        )
        closed = set()
        for e in entries:
            if e["id"] in closed:
                continue
            others = [o for o in (candidate.get(cat) or [])
                      if o["id"] != e["id"] and o["id"] not in closed and not o.get("safety")]
            target = _find_supersession_target(others, e.get("text", ""))
            if target is not None and target.get("ts", "") < e.get("ts", ""):
                if invalidate_entry(candidate, target["id"], by="consolidation:supersede"):
                    closed.add(target["id"])
                    ops.append(("superseded", _short(target.get("text"))))


def _build_summary(ops: list) -> str:
    """One human-readable line, grouped by op kind, in the founder's format."""
    order = ["closed", "merged", "superseded"]
    parts = []
    for kind in order:
        items = [detail for k, detail in ops if k == kind]
        if items:
            parts.append(f"{kind}: " + "; ".join(items))
    return " | ".join(parts)


def _build_diff(before: dict, after: dict) -> dict:
    """Structured before/after of the entries that left the live set this run."""
    before_ids = {e["id"]: (cat, e) for cat, e in _valid_entries(before)}
    after_ids = {e["id"] for _cat, e in _valid_entries(after)}
    removed = [{"id": eid, "category": cat, "text": e.get("text"),
                "uses": e.get("uses", 0), "ts": e.get("ts")}
               for eid, (cat, e) in before_ids.items() if eid not in after_ids]
    return {"removed": removed,
            "valid_before": len(before_ids), "valid_after": len(after_ids)}


def consolidate_user(user_id: int) -> dict:
    """Run one consolidation for one user. Returns a result dict (also persisted as
    a ConsolidationRun). Live memory is written only if the run changes something
    AND passes the bounded-delta guardrail."""
    now = datetime.now(timezone.utc)
    session = get_session()
    try:
        # Row-lock the user: no concurrent turn-write can race the candidate swap.
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return {"status": "no_user"}
        original = copy.deepcopy(user.user_profile_memory or {})
        candidate = copy.deepcopy(original)
        valid_before = _valid_count(candidate)

        ops: list = []
        allowed_safety_closures: set = set()
        _expire_ttl(candidate, ops, now)
        _supersede_safety(candidate, ops, allowed_safety_closures)
        _close_stale(candidate, ops, now)
        _merge_near_dupes(candidate, ops)
        _collapse_contradictions(candidate, ops)

        valid_after = _valid_count(candidate)
        removed = valid_before - valid_after

        # No change -> quiet night. Don't write, don't log a summary.
        if removed <= 0 and not ops:
            return {"status": "noop", "valid_before": valid_before}

        # Bounded-delta guardrail: a runaway pass never lands.
        fraction = (removed / valid_before) if valid_before else 0.0
        if valid_before > 0 and fraction > config.CONSOLIDATION_MAX_DELTA_FRACTION:
            logger.warning(
                "CONSOLIDATION_ABORT user=%s removed=%d/%d (%.0f%% > %.0f%% cap) — "
                "live memory unchanged, needs review",
                user_id, removed, valid_before, fraction * 100,
                config.CONSOLIDATION_MAX_DELTA_FRACTION * 100,
            )
            run = ConsolidationRun(
                user_id=user_id, valid_before=valid_before, removed_count=removed,
                aborted=True, summary=f"ABORTED: would remove {removed}/{valid_before} "
                f"({fraction:.0%} > {config.CONSOLIDATION_MAX_DELTA_FRACTION:.0%} cap)",
                diff=_build_diff(original, candidate), prev_profile=original,
            )
            session.add(run)
            session.commit()
            return {"status": "aborted", "removed": removed, "valid_before": valid_before,
                    "run_id": run.id}

        # Safety invariant (belt + suspenders): the ONLY safety entries allowed to
        # leave the live set are the trigger-audited supersession closures tracked
        # above — anything else is a dropped safety entry and aborts the run.
        before_safety = {e["id"] for _c, e in _valid_entries(original) if e.get("safety")}
        after_safety = {e["id"] for _c, e in _valid_entries(candidate) if e.get("safety")}
        assert before_safety - after_safety <= allowed_safety_closures, \
            "consolidation dropped a safety entry outside the audited supersession path"

        summary = _build_summary(ops)
        run = ConsolidationRun(
            user_id=user_id, valid_before=valid_before, removed_count=removed,
            aborted=False, summary=summary,
            diff=_build_diff(original, candidate), prev_profile=original,
        )
        # Copy-on-write swap: live memory replaced only now, atomically in the lock.
        user.user_profile_memory = candidate
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(user, "user_profile_memory")
        session.add(run)
        session.commit()
        logger.info("CONSOLIDATION user=%s %s", user_id, summary)
        return {"status": "ok", "removed": removed, "valid_before": valid_before,
                "summary": summary, "run_id": run.id}
    finally:
        session.close()


def rollback(user_id: int, run_id: int) -> bool:
    """Restore a user's profile to the snapshot a given run captured. Returns True
    on success. The escape hatch when a night's consolidation looks wrong."""
    session = get_session()
    try:
        run = session.get(ConsolidationRun, run_id)
        if not run or run.user_id != user_id or run.prev_profile is None:
            return False
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return False
        user.user_profile_memory = copy.deepcopy(run.prev_profile)
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(user, "user_profile_memory")
        session.commit()
        logger.warning("CONSOLIDATION_ROLLBACK user=%s run=%s", user_id, run_id)
        return True
    finally:
        session.close()


def consolidate_all():
    """Nightly sweep: consolidate every active user. Flag-gated."""
    if not config.CONSOLIDATION_ENABLED:
        return
    session = get_session()
    try:
        user_ids = [u.id for u in session.query(User).filter(User.active.is_(True)).all()]
    finally:
        session.close()
    for uid in user_ids:
        try:
            consolidate_user(uid)
        except Exception as e:
            logger.error("CONSOLIDATION_FAILED user=%s err=%s", uid, e, exc_info=True)

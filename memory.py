"""
Unified memory-injection surface + write-time hygiene for user_profile_memory.

Phase A0: no behavior change. Every read site that today touches `user.memory`
routes through `build_memory_block(user, agent_type)`. The helper reproduces
each call site's current output verbatim. This is the single switchable surface
that A8's USER_PROFILE_MEMORY_ENABLED feature flag will gate in later phases.

Agent types and their A0 behavior:
  - "coach"     -> coach.build_context memory block (with name + framing)
  - "training"  -> agents/training memory block (terse, name-only header)
  - "readiness" -> agents/readiness memory block (terse, name-only header)
  - "nutrition" -> "" (nutrition does not inject memory today)
  - "admin"     -> raw user.memory text (rendered into the admin Jinja template)

The nutrition empty-string behavior is load-bearing: A0 must NOT silently add
memory to nutrition. That policy decision lands in A4, behind the flag.

Phase A1 additions:
  - render_body_line(user): renders height/weight/body-fat from typed columns,
    skipping nulls so we never emit "Body: 5'7, None lbs".
  - render_dietary_line(user): renders BOTH diet and restrictions — load-bearing
    for safety, because A5's regex pre-pass routes whey/lactose/gluten allergies
    to User.restrictions and the nutrition agent's only path to see them is via
    this renderer. A "dietary line = just diet" implementation silently drops
    allergies from nutrition context — the exact failure mode this plan exists
    to prevent.

Phase A2/A3 additions:
  - CATEGORIES: the six allowed buckets (identity, constraints,
    training_preferences, communication_preferences, schedule, goals).
  - apply_facts(profile, facts): pure function that takes the current
    user_profile_memory dict + a list of extraction-output fact dicts and
    returns the updated dict plus a stats summary. Handles action verbs
    (add/update/skip), the replaces_text byte-exact safe-overwrite, the
    write-time dedup ladder, and eviction when caps are exceeded.
  - All caller has to do is read-modify-write the JSON column atomically
    under the row-lock (see A8).

Column-as-source-of-truth: extraction never writes body metrics (weight,
height, body fat) or dietary identity (diet, restrictions) into JSON. Those
have typed columns. A5's regex pre-pass + future logic routes there directly.
The Haiku prompt explicitly tells the model to skip those classes of fact.
"""

import logging
import re
import uuid
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.memory")


CATEGORIES = (
    "identity",
    "constraints",
    "training_preferences",
    "communication_preferences",
    "schedule",
    "goals",
)

# Validity windows: an invalidated entry is MOVED out of its category list into
# this bucket, so every live reader (render, dedup, caps) sees only valid entries
# with no per-reader filtering. Exempt from the injectable-size budget. Absence of
# `invalidated_at` == valid, so pre-existing profiles need no backfill.
HISTORY_KEY = "__history__"


COACH_EMPTY_BLOCK = (
    "## WHAT YOU REMEMBER ABOUT THIS USER\n"
    "Nothing accumulated yet — you're just getting to know them."
)


def render_body_line(user) -> str:
    """
    Render a "Body:" line from typed columns. Skips any null field so we never
    emit "Body: 5'7, None lbs". Returns empty string if every field is null
    (the caller should not inject the line at all in that case).

    Source of truth: User.height_ft, User.height_in, User.weight_lbs,
    User.body_fat_pct (and the WeightLog table for history, not rendered here).
    """
    parts = []
    if user.height_ft is not None:
        if user.height_in is not None:
            parts.append(f"{user.height_ft}'{user.height_in}")
        else:
            parts.append(f"{user.height_ft}'")
    if user.weight_lbs is not None:
        parts.append(f"{user.weight_lbs} lbs")
    if user.body_fat_pct is not None:
        parts.append(f"{user.body_fat_pct}% bf")
    if not parts:
        return ""
    return "Body: " + ", ".join(parts)


def render_dietary_line(user) -> str:
    """
    Render a "Dietary:" line from typed columns. Always reads BOTH diet AND
    restrictions — the restrictions column is where A5's regex pre-pass writes
    allergies (whey, lactose, gluten, "can't eat X"). Dropping it here drops
    allergies from nutrition context.

    Returns empty string if both fields are null.
    """
    diet = (user.diet or "").strip() or None
    restrictions = (user.restrictions or "").strip() or None
    if not diet and not restrictions:
        return ""
    pieces = []
    if diet:
        pieces.append(diet)
    if restrictions:
        pieces.append(f"restrictions: {restrictions}")
    return "Dietary: " + "; ".join(pieces)


# A4 — selective category injection per agent.
#
# Each agent gets a curated slice of user_profile_memory plus the typed
# columns relevant to its domain. Universal safety constraints
# (constraints with safety:true) are appended for every agent regardless
# of the map, via render_categories(include_safety_universal=True).
#
# Notable design decisions, codified in the Phase A plan:
#   - body line (height/weight/body fat) goes to training only.
#     Nutrition gets dietary + food_context instead; readiness doesn't need it.
#   - dietary line (diet + restrictions) goes to nutrition only.
#     This is the nutrition-scoped allergy decision: allergies reach nutrition,
#     injuries reach everyone. If an allergy ever becomes mid-set relevant,
#     add 'dietary' to the relevant agent's map explicitly.
#   - nutrition's previous A0 behavior was empty string; the new injection
#     lands here behind the USER_PROFILE_MEMORY_ENABLED flag.
CATEGORY_INJECTION_MAP = {
    "nutrition": {
        "categories": ("identity", "training_preferences", "goals"),
        "include_body_line": False,
        "include_dietary_line": True,
        "include_food_context": True,
    },
    "training": {
        "categories": ("identity", "constraints", "schedule", "goals",
                       "training_preferences"),
        "include_body_line": True,
        "include_dietary_line": False,
        "include_food_context": False,
    },
    "readiness": {
        "categories": ("constraints", "schedule", "goals"),
        "include_body_line": False,
        "include_dietary_line": False,
        "include_food_context": False,
    },
    "coach": {
        # Top-level coach / freeform / morning / evening: a small slice
        # plus universal safety.
        "categories": ("identity", "goals"),
        "include_body_line": False,
        "include_dietary_line": False,
        "include_food_context": False,
    },
    "admin": {
        # Debug view: render every category, both body and dietary lines.
        "categories": CATEGORIES,
        "include_body_line": True,
        "include_dietary_line": True,
        "include_food_context": True,
    },
}


def _compose_block_from_profile(user, agent_type: str):
    """
    Render the agent's slice of user_profile_memory + relevant typed-column
    lines + universal safety. Returns (text, injected_ids). Text is empty
    string if nothing renders.
    """
    spec = CATEGORY_INJECTION_MAP[agent_type]
    profile = user.user_profile_memory or {}
    cat_text, injected_ids = render_categories(
        profile, spec["categories"], include_safety_universal=True,
    )
    lines = []
    if cat_text:
        lines.append(cat_text)
    if spec["include_body_line"]:
        body = render_body_line(user)
        if body:
            lines.append(body)
    if spec["include_dietary_line"]:
        diet = render_dietary_line(user)
        if diet:
            lines.append(diet)
    if spec["include_food_context"] and (user.food_context or "").strip():
        lines.append(f"Food context: {user.food_context.strip()}")
    return ("\n".join(lines).strip(), injected_ids)


def _legacy_blob_or_empty(user, agent_type: str) -> str:
    """A0 fallback paths for each agent — used when the feature flag is off
    OR when the new JSON column is empty and there's a legacy blob to fall
    back to (per A7: 'inject old user.memory blob as a single legacy notes
    block')."""
    if agent_type == "coach":
        if user.memory:
            return (
                f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n"
                "These are permanent facts you've learned about this user over time. "
                "Reference them naturally when relevant — but never list them out or "
                "make the user feel surveilled.\n"
                f"{user.memory}"
            )
        return COACH_EMPTY_BLOCK
    if agent_type in ("training", "readiness"):
        if user.memory:
            return f"\n\n## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{user.memory}"
        return ""
    if agent_type == "nutrition":
        return ""
    if agent_type == "admin":
        return user.memory or ""
    raise ValueError(f"_legacy_blob_or_empty: unknown agent_type {agent_type!r}")


def build_memory_block_with_ids(user, agent_type: str):
    """
    Read-path entry point that ALSO returns the list of entry ids actually
    injected (so the caller can queue the async uses-bump from A4).

    Behavior:
      - If the feature flag is OFF: return legacy blob (no JSON injection,
        no injected ids).
      - If the flag is ON and the JSON profile has any usable content for
        this agent: render from JSON.
      - If the flag is ON but the JSON profile is effectively empty for
        this agent AND legacy user.memory is non-empty: fall back to legacy
        blob (per A7 — let the legacy blob's relevance decay; new facts
        accumulate in JSON).
      - If both are empty: render whatever the agent's empty-state is
        (coach has a 'nothing accumulated yet' line; everyone else is '').

    Returns (text, injected_ids).
    """
    if agent_type not in CATEGORY_INJECTION_MAP:
        raise ValueError(f"build_memory_block: unknown agent_type {agent_type!r}")

    if not config.USER_PROFILE_MEMORY_ENABLED:
        return (_legacy_blob_or_empty(user, agent_type), [])

    new_text, injected_ids = _compose_block_from_profile(user, agent_type)

    if new_text:
        # Wrap with the agent's framing so the prompt structure is consistent.
        if agent_type == "coach":
            framed = (
                f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n"
                "These are permanent facts you've learned about this user over time. "
                "Reference them naturally when relevant — but never list them out or "
                "make the user feel surveilled.\n"
                f"{new_text}"
            )
            return (framed, injected_ids)
        if agent_type in ("training", "readiness"):
            framed = (
                f"\n\n## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{new_text}"
            )
            return (framed, injected_ids)
        if agent_type == "nutrition":
            framed = (
                f"\n\n## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{new_text}"
            )
            return (framed, injected_ids)
        if agent_type == "admin":
            return (new_text, injected_ids)

    # JSON empty for this agent — fall back to legacy blob (or empty state).
    return (_legacy_blob_or_empty(user, agent_type), [])


def build_memory_block(user, agent_type: str) -> str:
    """Read-path entry point. Returns just the rendered text — callers that
    want the injected ids (for the async uses-bump) should use
    build_memory_block_with_ids instead. The 5 A0 caller sites use this form
    because they don't care about the ids."""
    text, _ids = build_memory_block_with_ids(user, agent_type)
    return text


# ─── A2/A3: action-verb writes + dedup ladder + eviction ─────────────────────

# Short words filtered before computing token-Jaccard similarity. Includes
# common English stopwords and unit suffixes that show up everywhere in fitness
# facts ("lbs", "g", "oz") and would inflate similarity scores.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "is", "it", "of", "on", "or", "that", "the", "to", "was",
    "with", "i", "my", "me", "user", "lbs", "lb", "g", "oz", "kg", "kgs",
    "min", "mins", "sec", "secs", "hr", "hrs",
})

_NUMERIC_RE = re.compile(r"-?\d+(?:\.\d+)?")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9.]+")


def _tokenize(text: str) -> set:
    """Lowercased token set with stopwords stripped — for Jaccard similarity."""
    return {t for t in (m.group(0).lower() for m in _TOKEN_RE.finditer(text or ""))
            if t and t not in _STOPWORDS}


def _numeric_tokens(text: str) -> set:
    """Numbers appearing in text — used by the numeric-divergence dedup guard."""
    return set(_NUMERIC_RE.findall(text or ""))


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_JACCARD_DEDUP_THRESHOLD = 0.75


def _find_duplicate(category_entries: list, candidate_text: str):
    """
    Dedup ladder (within-category):
      1. Exact text match -> dup
      2. Token-Jaccard >= 0.75 -> dup, UNLESS both sides contain numeric tokens
         that differ (then they're different facts — e.g. "trains 3 days/week"
         vs "trains 5 days/week" on schedule). The numeric-divergence guard's
         original PR/weight examples are moot now (body metrics live in typed
         columns), but the guard is cheap insurance against future fact types.

    Returns the duplicate entry dict or None.
    """
    cand_lower = (candidate_text or "").strip().lower()
    if not cand_lower:
        return None
    cand_tokens = _tokenize(candidate_text)
    cand_nums = _numeric_tokens(candidate_text)

    for entry in category_entries:
        existing = (entry.get("text") or "").strip().lower()
        if existing == cand_lower:
            return entry
        existing_tokens = _tokenize(entry.get("text", ""))
        if _jaccard(cand_tokens, existing_tokens) >= _JACCARD_DEDUP_THRESHOLD:
            existing_nums = _numeric_tokens(entry.get("text", ""))
            if cand_nums and existing_nums and cand_nums != existing_nums:
                continue  # numeric divergence -> not a duplicate
            return entry
    return None


def _nonnumeric_core(text: str) -> set:
    return {t for t in _tokenize(text) if not t.isdigit()}


def _find_supersession_target(entries: list, candidate_text: str):
    """
    Failure-5b: a valid same-category entry that is the SAME fact with an updated
    numeric value — 'trains 3 days/week' vs 'trains 5 days/week'. These are the
    near-matches _find_duplicate deliberately skips as "numeric divergence"; here
    we treat a UNIQUE such match as a supersession (recency wins) instead of
    letting the contradiction coexist. Safety entries are never supersession
    targets (they close only via explicit invalidate_entry). Zero or >1 match ->
    None (ambiguous -> plain add, never guess).
    """
    cand_nums = _numeric_tokens(candidate_text)
    if not cand_nums:
        return None
    cand_core = _nonnumeric_core(candidate_text)
    matches = []
    for e in entries:
        if e.get("safety"):
            continue
        e_nums = _numeric_tokens(e.get("text", ""))
        if e_nums and e_nums != cand_nums and \
                _jaccard(cand_core, _nonnumeric_core(e.get("text", ""))) >= _JACCARD_DEDUP_THRESHOLD:
            matches.append(e)
    return matches[0] if len(matches) == 1 else None


def _find_substring_match(entries: list, fragment: str):
    """
    Distinctive-substring update matching (replaces byte-exact): a valid entry
    whose text contains the fragment (or vice versa), case-insensitive. Requires
    a UNIQUE non-safety match; zero or >1 -> None so the caller falls back to the
    mismatch-logged-then-add path and never guesses.
    """
    frag = (fragment or "").strip().lower()
    if not frag:
        return None
    matches = []
    for e in entries:
        if e.get("safety"):
            continue
        etext = (e.get("text") or "").lower()
        if frag in etext or etext in frag:
            matches.append(e)
    return matches[0] if len(matches) == 1 else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_entry(text: str, safety: bool = False) -> dict:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "text": text.strip(),
        "ts": _now_iso(),
        "uses": 0,
    }
    if safety:
        entry["safety"] = True
    return entry


def _profile_total_chars(profile: dict) -> int:
    return sum(len(e.get("text", ""))
               for cat, entries in profile.items() if cat != HISTORY_KEY
               for e in (entries or []))


def _category_chars(entries: list) -> int:
    return sum(len(e.get("text", "")) for e in entries)


def _evict_one(profile: dict, *, prefer_category: str = None, user_id=None,
               reason: str = "global_cap") -> bool:
    """
    Evict the single lowest-value non-safety entry across all categories
    (or within `prefer_category` if given). Eviction order: lowest `uses`,
    then oldest `ts`. Returns True if anything was evicted.

    Never evicts safety:true. Logs every eviction with {user_id, category,
    text, reason} so we can tune caps.
    """
    candidates = []  # (uses, ts, category, idx, entry)
    targets = ([prefer_category] if prefer_category
               else [c for c in profile.keys() if c != HISTORY_KEY])
    for cat in targets:
        entries = profile.get(cat) or []
        for i, e in enumerate(entries):
            if e.get("safety"):
                continue
            candidates.append((e.get("uses", 0), e.get("ts", ""), cat, i, e))
    if not candidates:
        return False
    candidates.sort(key=lambda t: (t[0], t[1]))
    _, _, cat, idx, victim = candidates[0]
    logger.info(
        "MEMORY_EVICT user_id=%s category=%s text=%r reason=%s",
        user_id, cat, victim.get("text"), reason,
    )
    profile[cat].pop(idx)
    return True


def _enforce_caps(profile: dict, user_id=None) -> None:
    """
    Enforce per-category soft cap then global hard cap. Soft cap evicts within
    the overflowing category; hard cap evicts globally. Safety entries are
    never targeted; if a category is entirely safety entries, the cap is
    silently exceeded (correct — safety dominates the budget by design).
    """
    soft = config.USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP
    hard = config.USER_PROFILE_MEMORY_CHAR_LIMIT

    for cat in list(profile.keys()):
        if cat == HISTORY_KEY:
            continue
        while _category_chars(profile.get(cat) or []) > soft:
            if not _evict_one(profile, prefer_category=cat,
                              user_id=user_id, reason="category_soft_cap"):
                break

    while _profile_total_chars(profile) > hard:
        if not _evict_one(profile, user_id=user_id, reason="global_hard_cap"):
            break


def _ensure_categories(profile: dict) -> dict:
    """Make sure every known category exists as a list. Mutates and returns."""
    for cat in CATEGORIES:
        if cat not in profile:
            profile[cat] = []
        elif profile[cat] is None:
            profile[cat] = []
    return profile


def apply_facts(profile, facts, *, user_id=None) -> tuple:
    """
    Pure-function write path. Takes the current user_profile_memory dict (or
    None) and a list of extraction-output dicts of shape:

        {"action": "add"|"update"|"skip",
         "category": "<one of CATEGORIES>",
         "text": "fact text",
         "replaces_text": "exact existing text being superseded" | None,
         "safety_critical": bool}

    Returns (new_profile, stats) where stats is a dict counting
    {added, updated, skipped, deduped, mismatched, invalid}.

    Safety rules:
      - Unknown `action` -> skip (logged).
      - Unknown `category` -> skip (logged).
      - update with non-matching replaces_text -> fall back to add (logged as
        MEMORY_UPDATE_MISMATCH per the A2 spec) so we never silently overwrite
        the wrong entry.
      - Write-time dedup ladder runs on both add and update-fallback-to-add.
      - safety_critical=True facts get safety:true on the entry; they're never
        evicted and never deduped against non-safety entries.

    Caller must read-modify-write the JSON column atomically inside a row lock.
    """
    profile = dict(profile) if profile else {}
    profile = _ensure_categories(profile)

    stats = {"added": 0, "updated": 0, "skipped": 0,
             "deduped": 0, "mismatched": 0, "invalid": 0}

    for fact in (facts or []):
        action = (fact.get("action") or "").lower()
        category = fact.get("category")
        text = (fact.get("text") or "").strip()
        replaces_text = (fact.get("replaces_text") or "").strip() or None
        is_safety = bool(fact.get("safety_critical"))

        if not text:
            stats["invalid"] += 1
            continue
        if category not in CATEGORIES:
            logger.warning("MEMORY_INVALID_CATEGORY user_id=%s category=%r text=%r",
                           user_id, category, text)
            stats["invalid"] += 1
            continue
        if action not in ("add", "update", "skip"):
            logger.warning("MEMORY_INVALID_ACTION user_id=%s action=%r text=%r",
                           user_id, action, text)
            stats["skipped"] += 1
            continue

        if action == "skip":
            stats["skipped"] += 1
            continue

        entries = profile[category]

        if action == "update":
            if replaces_text:
                target = _find_substring_match(entries, replaces_text)
                if target is not None:
                    # Distinctive-substring match (was byte-exact). Invalidate the
                    # old value (history-preserving, auditable) and add the new one.
                    # Safety targets are excluded by _find_substring_match — a safety
                    # fact closes only via the explicit trigger-guarded invalidate.
                    invalidate_entry(profile, target["id"], by="superseded",
                                     trigger=(f"user_id:{user_id}" if user_id else "apply_facts_update"))
                    entries.append(_new_entry(text, safety=is_safety))
                    stats["updated"] += 1
                    continue
                logger.warning(
                    "MEMORY_UPDATE_MISMATCH user_id=%s category=%s "
                    "replaces_text=%r text=%r -> falling back to add",
                    user_id, category, replaces_text, text,
                )
                stats["mismatched"] += 1
            # fall through to add path

        # add (or update-fallback): run dedup ladder, then append.
        # Safety facts skip the dedup against non-safety; only safety-safety
        # exact match counts as dup (we don't want a non-safety dedup hit
        # to suppress a safety-elevated repeat of the same text).
        if is_safety:
            dup = next((e for e in entries
                        if e.get("safety") and (e.get("text") or "").strip().lower()
                        == text.lower()), None)
        else:
            dup = _find_duplicate(entries, text)

        if dup is not None:
            # Refresh the existing entry's timestamp & promote safety if needed.
            dup["ts"] = _now_iso()
            if is_safety and not dup.get("safety"):
                dup["safety"] = True
            stats["deduped"] += 1
            continue

        # Failure-5b supersession: a numeric-divergent near-match (same fact, new
        # value — "trains 3 days/week" -> "trains 5 days/week") is superseded, not
        # left to coexist. Non-safety only; a unique match required (ambiguous ->
        # plain add, never guess).
        if not is_safety:
            super_target = _find_supersession_target(entries, text)
            if super_target is not None:
                invalidate_entry(profile, super_target["id"], by="superseded_by_recency",
                                 trigger=(f"user_id:{user_id}" if user_id else "apply_facts_add"))
                entries.append(_new_entry(text, safety=is_safety))
                stats["updated"] += 1
                continue

        entries.append(_new_entry(text, safety=is_safety))
        stats["added"] += 1

    _enforce_caps(profile, user_id=user_id)
    return profile, stats


def render_categories(profile, categories, *, include_safety_universal=True):
    """
    Render a list of categories' entries as a single text block. Categories
    not in the profile are silently skipped. If include_safety_universal is
    True (default), all safety:true entries from `constraints` are appended
    regardless of whether 'constraints' is in `categories` — implementing the
    A4 "safety facts are always injected" rule.

    Returns (rendered_text, injected_ids). The injected_ids list lets the
    caller queue the async `uses` bump from A4.
    """
    profile = profile or {}
    seen_ids = set()
    lines = []
    injected_ids = []

    for cat in categories:
        for e in profile.get(cat) or []:
            if e.get("id") in seen_ids:
                continue
            seen_ids.add(e["id"])
            lines.append(f"- {e['text']}")
            injected_ids.append(e["id"])

    if include_safety_universal:
        safety_extras = []
        for e in profile.get("constraints") or []:
            if not e.get("safety"):
                continue
            if e.get("id") in seen_ids:
                continue
            seen_ids.add(e["id"])
            safety_extras.append(f"- {e['text']}")
            injected_ids.append(e["id"])
        if safety_extras:
            lines.append("")
            lines.append("Safety constraints (always remembered):")
            lines.extend(safety_extras)

    return "\n".join(lines), injected_ids


def bump_uses(profile, entry_ids) -> dict:
    """
    Increment `uses` for each id in entry_ids. Mutates and returns the profile.
    Per A4, this runs async in the daemon-thread path, not on the read path.
    """
    if not entry_ids:
        return profile
    target = set(entry_ids)
    for cat, entries in (profile or {}).items():
        if cat == HISTORY_KEY:
            continue
        for e in entries or []:
            if e.get("id") in target:
                e["uses"] = (e.get("uses") or 0) + 1
    return profile


def invalidate_entry(profile, entry_id, *, by, trigger=None, at=None) -> bool:
    """
    Invalidate a currently-valid entry by id: stamp lifecycle metadata and MOVE
    it into HISTORY_KEY (out of every live reader's path). Returns True if
    invalidated, False if not found OR rejected.

    Safety guard (load-bearing once the model gets write access in Phase 3): a
    `safety:true` entry may only be closed WITH a recorded `trigger` — the message
    id / actor that justified it. A safety closure without a trigger is REJECTED
    (the entry stays valid) and logged; a hallucinated invalidation of a real
    allergy/injury is the worst single write this system can make. Every genuine
    safety closure logs at WARNING so nightly consolidation can surface it.
    """
    for cat in list((profile or {}).keys()):
        if cat == HISTORY_KEY:
            continue
        entries = profile.get(cat) or []
        for i, e in enumerate(entries):
            if e.get("id") != entry_id:
                continue
            if e.get("safety") and not (trigger and str(trigger).strip()):
                logger.warning(
                    "SAFETY_INVALIDATION_REJECTED entry_id=%s by=%s reason=no_trigger text=%r",
                    entry_id, by, e.get("text"),
                )
                return False
            e["invalidated_at"] = at or _now_iso()
            e["invalidated_by"] = by
            e["invalidated_trigger"] = trigger
            profile.setdefault(HISTORY_KEY, []).append({**e, "category": cat})
            entries.pop(i)
            if e.get("safety"):
                logger.warning(
                    "SAFETY_INVALIDATION entry_id=%s by=%s trigger=%s text=%r",
                    entry_id, by, trigger, e.get("text"),
                )
            return True
    return False


# ─── A5: deterministic safety pre-pass ───────────────────────────────────────
#
# Runs on every inbound user message BEFORE the LLM extraction queues. The
# regex is a guaranteed floor: even if Haiku misses, drops the extraction call,
# or returns invalid JSON, safety-critical facts still land somewhere.
#
# Routing (column-as-source-of-truth, per A1):
#   - dietary identity (vegan/vegetarian/kosher/halal) -> User.diet
#   - specific allergies/intolerances -> User.restrictions (append, dedup)
#   - injuries / medical / doctor's orders -> JSON constraints, safety:true
#
# This is paired with the safety_critical field in A2's Haiku output. Both
# write paths converge on the same dedup ladder via apply_facts() for JSON
# entries, so a regex hit + a Haiku hit on the same fact result in one entry.

# Injury vocabulary — verb + object form. We capture surrounding context
# up to the next clause boundary (sentence terminator OR a coordinating
# conjunction like ", also" / ", and" / ", but"). Without the conjunction
# stop, a single message that mentions an injury and an allergy in one
# sentence ("I tweaked my shoulder, also I'm allergic to whey") would
# capture both into the injury constraint, duplicating the allergy across
# User.restrictions AND JSON constraints — violating column-as-source-of-truth.
_CLAUSE_STOP = r"(?:[.!?\n]|,\s*(?:also|and|but|plus)\b)"

_INJURY_RE = re.compile(
    r"\b("
    # Verb-leading: "tweaked my shoulder", "pulled my hamstring"
    r"(?:tweaked|hurts?|hurt|pulled|tore|torn|sprained|broke|broken|strained|"
    r"injured|aching|stiff|jammed|wrecked|messed\s+up|screwed\s+up|fucked\s+up)"
    rf"(?:(?!{_CLAUSE_STOP}).){{0,80}}"
    r"|"
    # "bad knee/shoulder/back/..." possessive form
    r"bad\s+(?:knee|shoulder|back|hip|wrist|ankle|elbow|neck)"
    rf"(?:(?!{_CLAUSE_STOP}).){{0,40}}"
    r"|"
    # Body-part-leading complaint: "shoulder's killing me", "my knees are sore",
    # "back is acting up", "hip's been bothering me". Added because verb-
    # leading patterns miss natural SMS phrasing like "my shoulder's killing me"
    # which is exactly the kind of injury report a goodnight-bypass would drop.
    # Allow up to ~20 chars of auxiliary words (is, are, been, has been, etc.)
    # between the body part and the complaint verb.
    r"(?:knees?|shoulders?|backs?|hips?|wrists?|ankles?|elbows?|necks?|"
    r"hamstrings?|quads?|calves?|biceps?|triceps?|delts?|lats?|traps?|"
    r"glutes?|abs?|pecs?|forearms?)"
    r"[\s'a-z]{0,20}?"  # non-greedy gap for possessive + auxiliaries
    r"\b(?:killing|aching|sore|acting\s+up|bothering|hurting|tight|"
    r"throbbing|locked\s+up|shot|toast|wrecked)\b"
    rf"(?:(?!{_CLAUSE_STOP}).){{0,40}}"
    r")",
    re.IGNORECASE,
)

# Allergy / intolerance — specific foods or dietary intolerances.
# Captures the substance for the User.restrictions append.
_ALLERGY_RE = re.compile(
    r"\b("
    r"allergic\s+to\s+\w+(?:\s+\w+)?"
    r"|"
    r"(?:lactose|gluten|nut|dairy|shellfish|soy|egg|peanut|tree[- ]?nut)"
    r"[- ]?(?:intolerant|intolerance|allergy|allergic|free)"
    r"|"
    r"can(?:'t|not)\s+eat\s+\w+(?:\s+\w+)?"
    r")\b",
    re.IGNORECASE,
)

# Medical conditions worth flagging permanently.
_MEDICAL_RE = re.compile(
    r"\b(pregnant|diabetic|diabetes|asthma|asthmatic|heart\s+condition|"
    r"kidney\s+(?:stones?|disease|issue|problem)|high\s+blood\s+pressure|"
    r"hypertension|epilep(?:sy|tic))\b",
    re.IGNORECASE,
)

# Dietary identity — these go to User.diet, not to constraints.
_DIET_IDENTITY_RE = re.compile(
    r"\b(vegan|vegetarian|pescatarian|kosher|halal|carnivore|keto)\b",
    re.IGNORECASE,
)

# Doctor's orders / PT recommendations — wide context window because the
# pattern is "person + told/said + advice" with up to ~40 chars between.
# Stops at clause boundaries so it doesn't sweep across into other facts.
_DOCTOR_RE = re.compile(
    r"\b(doctor|doc|physical\s+therapist|pt|chiropractor|surgeon)\b"
    rf"(?:(?!{_CLAUSE_STOP}).){{0,40}}"
    r"\b(told|said|recommended|advised|warned|wants)\b"
    rf"(?:(?!{_CLAUSE_STOP}).){{0,80}}",
    re.IGNORECASE,
)


def _extract_substring(match) -> str:
    """Return the trimmed full match text — what we'll persist as the fact."""
    return match.group(0).strip().rstrip(".,!?;:").strip()


def detect_safety_signals(user_message: str) -> dict:
    """
    Pure function. Scan an inbound user message and return a routing decision:

        {
            "diet": "vegan" | None,                # set User.diet
            "restrictions_append": [str, ...],     # append-and-dedup to User.restrictions
            "constraints": [str, ...],             # JSON entries, safety:true
        }

    Empty fields mean no action. Caller writes them under a row lock.
    All matches are case-insensitive; persisted text is the verbatim match.
    """
    text = user_message or ""
    out = {"diet": None, "restrictions_append": [], "constraints": []}

    diet_match = _DIET_IDENTITY_RE.search(text)
    if diet_match:
        out["diet"] = diet_match.group(1).lower()

    for m in _ALLERGY_RE.finditer(text):
        snippet = _extract_substring(m)
        if snippet and snippet.lower() not in (s.lower() for s in out["restrictions_append"]):
            out["restrictions_append"].append(snippet)

    for regex in (_INJURY_RE, _MEDICAL_RE, _DOCTOR_RE):
        for m in regex.finditer(text):
            snippet = _extract_substring(m)
            if snippet and snippet.lower() not in (s.lower() for s in out["constraints"]):
                out["constraints"].append(snippet)

    return out


def _append_to_restrictions(existing: str, new_items: list) -> str:
    """
    Append new restriction snippets to User.restrictions (a free-form Text
    column) with case-insensitive substring dedup. Preserves whatever
    formatting the user already had — we just join new items with '; '.
    """
    existing = (existing or "").strip()
    existing_lower = existing.lower()
    additions = []
    for item in new_items:
        item_l = item.lower().strip()
        if not item_l:
            continue
        if item_l in existing_lower:
            continue
        if any(item_l in a.lower() for a in additions):
            continue
        additions.append(item.strip())
    if not additions:
        return existing
    if existing:
        return existing + "; " + "; ".join(additions)
    return "; ".join(additions)


def apply_safety_signals_task(user_id: int, user_message: str):
    """
    Background-thread entry point: run the regex pre-pass against a message
    and persist any matches under the row lock. Idempotent — running twice
    on the same message is a no-op because every write path dedupes.

    Routing follows A1's column-as-source-of-truth rule:
      - diet -> User.diet (overwrite only if user.diet is empty; never clobber
        an existing diet because that's a hard preference the user set)
      - allergies/intolerances -> append to User.restrictions, dedup
      - injuries/medical/doctor's orders -> apply_facts() to JSON constraints
        with safety_critical=True, sharing the dedup ladder + cap enforcement
    """
    signals = detect_safety_signals(user_message)
    if (not signals["diet"] and not signals["restrictions_append"]
            and not signals["constraints"]):
        return

    from models import get_session, User as _User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        user = (session.query(_User)
                .filter(_User.id == user_id)
                .with_for_update()
                .one_or_none())
        if not user:
            return

        changed = False
        json_changed = False

        # diet: never overwrite a user-set value (it could be a free-form
        # phrase like "omnivore, mostly chicken"); only set if empty.
        if signals["diet"] and not (user.diet or "").strip():
            user.diet = signals["diet"]
            changed = True
            logger.info("SAFETY_REGEX_DIET user_id=%s value=%s", user_id, signals["diet"])

        if signals["restrictions_append"]:
            new_text = _append_to_restrictions(
                user.restrictions, signals["restrictions_append"]
            )
            if new_text != (user.restrictions or ""):
                user.restrictions = new_text
                changed = True
                logger.info(
                    "SAFETY_REGEX_RESTRICTIONS user_id=%s appended=%s",
                    user_id, signals["restrictions_append"],
                )

        if signals["constraints"]:
            facts = [
                {"action": "add", "category": "constraints",
                 "text": snippet, "replaces_text": None,
                 "safety_critical": True}
                for snippet in signals["constraints"]
            ]
            current = dict(user.user_profile_memory or {})
            new_profile, stats = apply_facts(current, facts, user_id=user_id)
            if stats["added"] > 0 or stats["deduped"] > 0:
                user.user_profile_memory = new_profile
                changed = True
                json_changed = True
                logger.info(
                    "SAFETY_REGEX_CONSTRAINTS user_id=%s added=%d deduped=%d",
                    user_id, stats["added"], stats["deduped"],
                )

        if json_changed:
            flag_modified(user, "user_profile_memory")
        if changed:
            session.commit()
    except Exception as e:
        logger.warning("SAFETY_REGEX_FAILED user_id=%s err=%s", user_id, e)
    finally:
        session.close()


# ─── A6: delivered coaching points (repetition guard) ───────────────────────
#
# A running list of substantive coaching points already delivered in past
# turns — "explained progressive overload", "set deload week of 6/9". Injected
# into every coach prompt so the model can REFERENCE rather than RE-DELIVER.
#
# Stored as a single Text column (User.delivered_coaching_points), capped at
# COACHING_POINTS_CHAR_LIMIT. Newest-first ordering: when we exceed the cap
# we drop from the tail (oldest points first). One bullet per line, terse.

_COACHING_POINT_MAX_LEN = 120  # per-line guard so one verbose Haiku output can't eat the whole cap


def merge_coaching_points(existing: str, new_points) -> str:
    """
    Pure function. Take the existing delivered_coaching_points text and a list
    of new tag strings; prepend the new tags (newest first), dedup case-
    insensitively, and trim from the tail to fit COACHING_POINTS_CHAR_LIMIT.

    Returns the new text. Empty string if nothing accumulated.
    """
    existing = (existing or "").strip()
    existing_lines = [ln.strip() for ln in existing.split("\n") if ln.strip()]
    existing_lower = {ln.lower() for ln in existing_lines}

    additions = []
    for raw in (new_points or []):
        line = (raw or "").strip()
        if not line:
            continue
        if len(line) > _COACHING_POINT_MAX_LEN:
            line = line[:_COACHING_POINT_MAX_LEN].rstrip() + "…"
        low = line.lower()
        if low in existing_lower:
            continue
        if any(low == a.lower() for a in additions):
            continue
        additions.append(line)

    combined = additions + existing_lines

    cap = config.COACHING_POINTS_CHAR_LIMIT
    while combined and sum(len(s) + 1 for s in combined) > cap:
        combined.pop()  # drop oldest

    return "\n".join(combined)


def extract_and_store_coaching_points_task(user_id: int, user_message: str, coach_response: str):
    """
    Background daemon task. Ask Haiku to identify any SUBSTANTIVE coaching
    points the coach delivered in this turn (explanations, plan decisions,
    rules, calculations) — NOT logistics, NOT acknowledgments, NOT small
    talk. Persist them to User.delivered_coaching_points under the row lock.

    Tags are short ("explained progressive overload", "set deload week of 6/9")
    so the coach can scan past delivered points cheaply and reference rather
    than repeat.
    """
    import anthropic
    import json
    from models import get_session, User as _User

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are tagging substantive coaching points delivered in a single SMS coaching turn. The goal is to prevent the coach from re-explaining the same thing in future messages.

A "coaching point" is something the coach EXPLAINED, DECIDED, or PRESCRIBED — not chat:
  YES — "explained progressive overload"
  YES — "set deload week starting Jun 9"
  YES — "calculated 2200 cal target"
  YES — "recommended Romanian deadlifts as deadlift alternative"
  YES — "warned about overhead pressing with shoulder issue"
  NO  — "asked how the workout went" (it's a question, not a point)
  NO  — "praised today's effort" (acknowledgment, not substance)
  NO  — "said good morning" (small talk)

User said: "{user_message}"
Coach said: "{coach_response}"

Return ONLY valid JSON:
  {{"points": ["short tag 1", "short tag 2"]}}

Rules:
  - Each tag is a terse statement starting with a verb ("explained", "set", "recommended", "warned", "calculated", "prescribed").
  - One tag per substantive point. Most turns will have 0 or 1.
  - Keep each tag under 100 characters.
  - If the coach gave no substantive coaching content (just acknowledged, asked, or chatted), return: {{"points": []}}
"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        # Defer import to avoid circular: memory.py is imported by app.py which
        # also imports cost_tracking. cost_tracking imports models lazily.
        from cost_tracking import track
        track(user_id, "memory.extract_coaching_points",
              "claude-haiku-4-5-20251001", response.usage)
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in raw:
            raw = raw[:raw.rindex("}") + 1]
        data = json.loads(raw)
        points = data.get("points", [])
        if not points:
            return

        session = get_session()
        try:
            user = (session.query(_User)
                    .filter(_User.id == user_id)
                    .with_for_update()
                    .one_or_none())
            if not user:
                return
            new_text = merge_coaching_points(user.delivered_coaching_points, points)
            if new_text != (user.delivered_coaching_points or ""):
                user.delivered_coaching_points = new_text
                session.commit()
                logger.info(
                    "COACHING_POINTS_UPDATE user_id=%s added=%d total_chars=%d",
                    user_id, len(points), len(new_text),
                )
        finally:
            session.close()
    except Exception as e:
        logger.warning("COACHING_POINTS_FAILED user_id=%s err=%s", user_id, e)


def update_memory_uses_task(user_id: int, entry_ids):
    """
    Background daemon-thread task: bump `uses` for the given entry ids on the
    user's user_profile_memory column. Acquires the same SELECT ... FOR UPDATE
    row lock as the extraction write path, so concurrent extractions and
    uses-bumps serialize on a single critical section per user.

    Imported here (not at module top) to avoid a circular import with models.

    Uses flag_modified because SQLAlchemy's JSON column tracker treats nested
    mutations as a no-op (it sees the same dict identity assigned and doesn't
    issue an UPDATE). Without flag_modified, the increment is invisibly lost.
    """
    if not entry_ids:
        return
    from models import get_session, User as _User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        user = (session.query(_User)
                .filter(_User.id == user_id)
                .with_for_update()
                .one_or_none())
        if not user:
            return
        current = dict(user.user_profile_memory or {})
        bump_uses(current, entry_ids)
        user.user_profile_memory = current
        flag_modified(user, "user_profile_memory")
        session.commit()
    except Exception as e:
        logger.warning("MEMORY_USES_BUMP_FAILED user_id=%s err=%s", user_id, e)
    finally:
        session.close()

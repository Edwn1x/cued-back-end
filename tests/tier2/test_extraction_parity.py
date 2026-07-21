"""
Tier-2 — extraction→remember RECALL PARITY. This is Gate B's automated green light:
per-turn LLM extraction may retire only once `remember` (the loop's memory tool)
recalls stated facts at least as well as extraction did.

Built now, evidence-independent: it runs AUTOMATICALLY the moment a parity dataset
exists and SKIPS with a clear reason until then, so CI stays green and the comparison
fires as soon as a live user has accumulated parallel writes. It calls the legacy
extraction path directly, so it must run while extraction still exists (i.e. before the
Gate B deletion commit lands) — which is exactly when its verdict is needed.

## Dataset contract
Point `CUED_PARITY_DATASET` at a JSON file (a scrubbed live export):

    [
      {"messages": [{"role": "user"|"coach", "text": "..."}, ...],
       "expected_facts": ["trains 5 days a week", "allergic to peanuts", ...]},
      ...
    ]

Each object is one conversation + the facts a good memory should recall from it. We
replay each conversation TWO ways into two fresh users — extraction-only and
remember-only — then score recall of `expected_facts` against each rendered profile.

Parity passes when remember-only recall >= extraction-only recall - EPSILON.
Run: CUED_PARITY_DATASET=path/to/data.json pytest --run-tier2 -s tests/tier2/test_extraction_parity.py
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.tier2

EPSILON = 0.05  # remember may not recall MORE than 5 points worse than extraction


def _load_dataset():
    path = os.getenv("CUED_PARITY_DATASET")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rendered_memory(user_id: int) -> str:
    from models import get_session, User
    from memory import render_categories, CATEGORIES
    s = get_session()
    try:
        prof = s.get(User, user_id).user_profile_memory or {}
    finally:
        s.close()
    text, _ids = render_categories(prof, CATEGORIES, include_safety_universal=True)
    return text


def _replay_extraction(db, convo) -> int:
    """Legacy path: for each user turn, run the per-turn LLM extraction."""
    from tests.factories import make_user
    from app import extract_and_store_memory
    user = make_user(db)
    pending_user = None
    for m in convo["messages"]:
        if m["role"] == "user":
            pending_user = m["text"]
        elif m["role"] == "coach" and pending_user is not None:
            extract_and_store_memory(user.id, pending_user, m["text"])
            pending_user = None
    return user.id


def _replay_remember(db, monkeypatch, convo) -> int:
    """New path: run the loop (remember on, extraction off) over each user turn."""
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)
    user = make_user(db)
    for m in convo["messages"]:
        if m["role"] == "user":
            run_agent_loop(user, m["text"], "freeform")
    return user.id


def _recall_score(memory_text: str, expected_facts: list) -> float:
    """Fraction of expected facts represented in the rendered memory. Model-judged
    (semantic, not substring) so paraphrase counts as recall."""
    if not expected_facts:
        return 1.0
    import anthropic, config
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    facts = "\n".join(f"{i}. {f}" for i, f in enumerate(expected_facts))
    prompt = (
        "Here is a coach's memory of a user:\n---\n" + (memory_text or "(empty)") +
        "\n---\nFor each numbered fact below, answer 1 if it is represented in the memory "
        "(paraphrase is fine), else 0. Reply ONLY as a JSON array of 0/1, in order.\n\n" + facts
    )
    resp = client.messages.create(
        model=config.HAIKU_MODEL, max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "[]")
    try:
        marks = json.loads(text[text.index("["): text.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        marks = []
    marks = [int(bool(x)) for x in marks][: len(expected_facts)]
    return sum(marks) / len(expected_facts)


def test_remember_reaches_recall_parity_with_extraction(db, monkeypatch):
    dataset = _load_dataset()
    if dataset is None:
        pytest.skip("no CUED_PARITY_DATASET — Gate B clock hasn't started (needs live "
                    "parallel writes). This eval fires automatically once the dataset exists.")

    ext_scores, rem_scores = [], []
    for convo in dataset:
        facts = convo.get("expected_facts", [])
        ext_uid = _replay_extraction(db, convo)
        ext_scores.append(_recall_score(_rendered_memory(ext_uid), facts))
        rem_uid = _replay_remember(db, monkeypatch, convo)
        rem_scores.append(_recall_score(_rendered_memory(rem_uid), facts))

    ext = sum(ext_scores) / len(ext_scores)
    rem = sum(rem_scores) / len(rem_scores)
    print(f"\n[PARITY] extraction recall={ext:.2%}  remember recall={rem:.2%}  "
          f"(n={len(dataset)} conversations)")
    print("[PARITY] Gate B: extraction may retire when remember >= extraction - "
          f"{EPSILON:.0%}. verdict: {'PASS' if rem >= ext - EPSILON else 'NOT YET'}")

    assert rem >= ext - EPSILON, (
        f"remember recall {rem:.2%} regresses vs extraction {ext:.2%} beyond {EPSILON:.0%} — "
        f"do NOT retire extraction yet"
    )

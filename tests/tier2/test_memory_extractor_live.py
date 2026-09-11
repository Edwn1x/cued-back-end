"""
Tier-2 (live) — replay the founder's REAL messages from 2026-09-11 through the
memory extractor's model half and judge the raw output (before the sanitizer, so
this measures the model + prompt, not the backstop).
Run: pytest tests/tier2/test_memory_extractor_live.py --run-tier2 -s
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2

_WD_DATE = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
                      r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}


def _facts(db, user, msg, reply):
    from app import extract_memory_facts
    out = extract_memory_facts(user.id, msg, reply)
    print(f"\n[MEM] user: {msg[:70]!r}\n[MEM] facts: {[(f.get('category'), f.get('text')) for f in (out or []) if f.get('action') != 'skip']}")
    return [f for f in (out or []) if f.get("action") != "skip"]


def test_complaint_yields_no_fragment_and_no_constraint(db):
    user = make_user(db, name="Nau", current_split="ppl")
    facts = _facts(db, user, "Yeah but lowkey Thursday is my most stacked day\nYou can see it on the screenshot\nSee why my gym schedule is all messed up",
                   "yeah thursday is brutal — c104 discussion 12:30-2, cs70 3:30-5, engin 183 6-8. want me to slot thursday as a rest day?")
    for f in facts:
        assert len(f["text"].split()) >= 3, f
        assert f.get("category") != "constraints", f"a complaint is not a constraint: {f}"


def test_direct_identity_statement_is_stored(db):
    user = make_user(db, name="Nau")
    facts = _facts(db, user, "Lol im cs", "lol based on 61c, 70, and data c104? you're either cs or data science. am i close?")
    ids = [f for f in facts if f.get("category") == "identity"]
    assert ids and any("cs" in f["text"].lower() for f in ids), facts


def test_yesterday_is_dated_correctly_or_kept_recurring(db):
    user = make_user(db, name="Nau", user_timezone="America/Los_Angeles", current_split="ppl")
    tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(tz).date()
    yday = today - timedelta(days=1)
    facts = _facts(db, user, "Hmm, well funny enough yesterday I did end up going to the gym from 9-11\nI hit pull",
                   "ok so thursday's not automatically dead — you just found a 9-11 window. logged the pull sesh.")
    for f in facts:
        for m in _WD_DATE.finditer(f["text"]):
            d = date(int(m.group(4)), _MONTHS[m.group(2).lower()[:3]], int(m.group(3)))
            assert d.strftime("%A").lower() == m.group(1).lower(), f"weekday/date disagree: {f['text']}"
            assert d in (yday, today), f"'yesterday' resolved to {d}, expected {yday}: {f['text']}"

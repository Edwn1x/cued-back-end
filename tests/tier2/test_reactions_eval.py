"""
Tier-2 (live) — WHEN to react / thread, on eight real bursts from the founder's
2026-09-11 conversation, through the REAL coach loop with react_to_message and
reply_in_thread offered and the sidecar mocked. Writes
rewrite/evals/reactions-and-replies.md for hand review.

Founder's bar: react when a friend would, stay silent when a friend wouldn't, thread
only where a paragraph would be ambiguous — and after a reaction-only turn the
unanswered count and every silence gate are unchanged (the rule that bites).
Mechanical floor per case is in EXPECT; the rest is the hand read.

Run: pytest tests/tier2/test_reactions_eval.py --run-tier2 -s
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "rewrite", "evals", "reactions-and-replies.md")

# (label, prior coach line, burst (list of their texts), expectations)
#   expect: react ∈ {"yes","no","either"}, text ∈ {"yes","no","either"}, thread ∈ {"no","either"}, emoji_in (set|None)
CASES = [
    ("bare ack", "go study. i'll check in at noon.", ["Right right"],
     dict(react="yes", text="no", thread="no", emoji_in={"👍", "❤️"})),
    ("ack + sign-off", "close the tab bro. 90 min of discrete, then reward yourself. go.", ["Alr alr", "I'm on it bro"],
     dict(react="yes", text="no", thread="no", emoji_in={"👍", "❤️", "‼️"})),
    ("funny story", "you planning to hit the gym after the quiz or is today a wash?",
     ["No yeah the late night gym sesh was great", "I was feeling tired at first", "And I was already lwk debating not going",
      "But then one of the brothers at the frat had already told me he wanted to go", "So I texted him if he was still down since his class ended at 9pm",
      "And he was like \"yeah im ready lets go\"", "So I was like \"alr wtv, might as well\"", "Even tho I had to study for my quiz ngl 😬"],
     dict(react="either", text="yes", thread="no", emoji_in={"😂", "‼️", "❤️"})),
    ("nerves", "go lock in — text me after the quiz and tell me how it went.", ["Hopefully I do good"],
     dict(react="either", text="yes", thread="no", emoji_in={"❤️", "‼️", "👍"})),
    ("multi-topic burst", "you a load-it-with-meat person or do you actually get greens in there?",
     ["Lowkey I get mostly meat tho, some greens, but I'm tryna get my moneys worth ngl", "It's kinda expensive ngl", "Like 25 dollars per bowl",
      "Also I'm a junior so I don't got the dining hall pass like freshman's do", "Rn prolly like 3-4 days a week for the gym", "It used to be 5-6, but lowkey im getting more busy"],
     dict(react="either", text="yes", thread="either", emoji_in=None)),
    ("a question", "quiz is at 4. that's your hard deadline whether you study or not.", ["Why does everyone think that c104 means data science?"],
     dict(react="no", text="yes", thread="no", emoji_in=None)),
    ("correction", "here's what i'm working with … 2450 cal and 139g protein. sound right?", ["Hmm, 2450 sounds kinda high for losing fat no?"],
     dict(react="no", text="yes", thread="no", emoji_in=None)),
    ("workout without detail", "you gonna hit the gym after or is today a wash", ["hit pull"],
     dict(react="either", text="either", thread="no", emoji_in={"👍", "‼️", "❤️"})),
]


@pytest.fixture
def sidecar(monkeypatch):
    import requests
    calls: list = []

    class _R:
        def __init__(self, payload, status=200):
            self._p, self.status_code, self.text = payload, status, str(payload)
        def json(self):
            return self._p

    def _post(url, json=None, headers=None, timeout=None):
        route = url.rsplit("/", 1)[-1]
        calls.append((route, json))
        return _R({"ok": True, "provider_message_id": f"spc-{route}-{len(calls)}"})
    monkeypatch.setattr(requests, "post", _post)
    return calls


def test_when_to_react_and_thread(db, sidecar, monkeypatch):
    import config, app
    from tests.factories import make_user
    from models import get_session, Message, User
    from engagement_tracker import increment_unanswered, has_unanswered_outbound
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.railway.internal:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")
    monkeypatch.setattr(config, "IMESSAGE_REACTIONS_ENABLED", True)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", False)
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", False)

    rows_out = []
    failures = []
    for label, prior, burst, exp in CASES:
        sidecar.clear()
        user = make_user(db, name="Nau", preferred_channel="imessage", onboarding_step=3,
                         current_split="ppl", goal="fat_loss,muscle_building")
        s = get_session()
        try:
            base = datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(Message(user_id=user.id, direction="out", body=prior, message_type="freeform",
                          channel="imessage", provider_sid="spc-prior", delivery_status="sent",
                          created_at=base - timedelta(minutes=3)))
            for i, t in enumerate(burst):
                s.add(Message(user_id=user.id, direction="in", body=t, message_type="freeform",
                              channel="imessage", provider_sid=f"spc-in-{i}", delivery_status="delivered",
                              created_at=base - timedelta(seconds=60 - i * 5)))
            s.commit()
        finally:
            s.close()

        app.process_buffered_message(user.id, "\n".join(burst), "freeform")

        reacts = [j for r, j in sidecar if r == "react"]
        sends = [j for r, j in sidecar if r == "send"]
        reacted = bool(reacts)
        texted = bool(sends)
        threaded = any("reply_to" in j for j in sends)
        emoji = reacts[0]["emoji"] if reacts else None
        from sms import TAPBACKS
        shown = TAPBACKS.get((emoji or "").lower(), emoji)
        # the rule that bites: after the turn, silence gates are unchanged
        increment_unanswered(user.id)
        db.expire_all()
        u = db.get(User, user.id)
        strike_free = (u.unanswered_count or 0) == 0 or texted  # a TEXT is allowed to count; a reaction never
        gates_ok = (not reacted or texted) or (has_unanswered_outbound(user.id) is False)

        checks = {}
        if exp["react"] != "either":
            checks["react"] = (reacted == (exp["react"] == "yes"))
        if exp["text"] != "either":
            checks["text"] = (texted == (exp["text"] == "yes"))
        if exp["thread"] == "no":
            checks["no thread"] = not threaded
        if reacted and exp["emoji_in"]:
            checks["emoji fits"] = shown in exp["emoji_in"]
        checks["≤1 reaction"] = len(reacts) <= 1
        checks["never a strike"] = strike_free and gates_ok
        for k, ok in checks.items():
            if not ok:
                failures.append(f"[{label}] {k}: reacted={reacted}({shown}) texted={texted} threaded={threaded} sends={[x['text'][:80] for x in sends]}")
        rows_out.append((label, burst, shown if reacted else None, sends, threaded, checks))
        print(f"\n[{label}] react={shown if reacted else '—'} text={'yes' if texted else 'no'} thread={threaded}"
              + (f"\n[{label}] coach: {sends[0]['text'][:200]}" if sends else ""))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# Reactions + threaded replies — live eval\n\n")
        f.write(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} through the real coach loop "
                f"(`{config.AGENT_LOOP_MODEL}`), tools react_to_message + reply_in_thread offered, sidecar mocked.\n\n")
        f.write("Founder's bar: react when a friend would, stay silent when a friend wouldn't, thread only where a paragraph "
                "would be ambiguous; a reaction never counts as unanswered.\n\n")
        for label, burst, shown, sends, threaded, checks in rows_out:
            f.write(f"## {label}\n\n**them:** " + " / ".join(burst) + "\n\n")
            f.write(f"**reaction:** {shown or '—'}  \n**text:** {sends[0]['text'] if sends else '— (reaction only)'}  \n"
                    f"**threaded:** {'yes → ' + sends[0]['reply_to'] if threaded else 'no'}\n\n")
            f.write("mechanical: " + " · ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items()) + "\n\nhand review: _pending_\n\n")
    print(f"\n[EVAL] wrote {OUT}")
    assert not failures, "\n".join(failures)

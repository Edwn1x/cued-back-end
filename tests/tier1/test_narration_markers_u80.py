"""
Planning notes texted to a user — the 2026-10-28 16:14 leak (user 80, msg 7922).

The model wrote its plan as the reply:

    no need to apologize. short reply, no question needed. maybe "no worries, text works
    fine. next time a pic if u think of it". already said pic is easier; don't repeat.

    no worries, the text was enough. it's all logged

One agent_loop call, end_turn, 93 tokens, texted verbatim. Only ONE planning marker hit
("short reply"), so looks_like_narration stayed False and the salvage never ran — even
though the text ends on a clean direct line the salvage would have kept. The idiom family
this time: "no question needed", "already said …", "don't repeat", and a quoted draft
introduced by "maybe". Each is something a coach could text once; two together is a plan.
"""
from __future__ import annotations

import pytest

U80_7922 = ('no need to apologize. short reply, no question needed. maybe "no worries, text works fine. '
            'next time a pic if u think of it". already said pic is easier; don\'t repeat.\n\n'
            'no worries, the text was enough. it\'s all logged')
U80_7922_REPLY = "no worries, the text was enough. it's all logged"


def test_the_live_leak_is_caught_and_the_direct_line_is_salvaged():
    from agent_tools import looks_like_narration, salvage_direct_reply, _planning_marker_hits
    assert _planning_marker_hits(U80_7922) >= 2
    assert looks_like_narration(U80_7922)
    assert salvage_direct_reply(U80_7922) == U80_7922_REPLY


@pytest.mark.parametrize("plan", [
    "no question needed. already said pic is easier.",
    "already said that; don't repeat. keep it short",
    'maybe "no worries, text works fine". no question needed.',
    "short reply. don't repeat the pic thing.",
])
def test_the_new_markers_cluster_into_a_plan(plan):
    from agent_tools import looks_like_narration
    assert looks_like_narration(plan), plan


@pytest.mark.parametrize("line", [
    "already said it but: eat before class",
    "don't repeat yesterday, get the protein in early",
    "no question needed, just text me 'done' after",
    'maybe "ill do it tomorrow" is the honest answer lol',
    "short reply from me today, i'm in lab till 7",
    "no worries, the text was enough. it's all logged",
])
def test_one_marker_is_still_something_a_coach_texts(line):
    from agent_tools import looks_like_narration
    assert not looks_like_narration(line), line


def test_the_salvaged_tail_cannot_carry_a_new_marker():
    from agent_tools import salvage_direct_reply
    plan = "short reply, no question needed."
    assert salvage_direct_reply(plan + "\n\nalready said it, don't repeat.") is None

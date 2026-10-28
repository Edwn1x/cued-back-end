"""
Live 2026-10-28, user 77. The coach swapped his over-the-chair stretch (a Steelcase reclines) for a
standing wall/counter hinge and wall angels. He texted "reference vids pls". The coach replied "my
search didn't come back with anything just now" and scheduled a lookup task for 2:21. That run came
back empty too, and code sent him "couldn't find it — find short PT/coach reference videos for chin
tucks, doorway pec stretch, standing counter chest stretch, and wall angels.. want me to try again
later?" — the model-written goal pasted verbatim.

Three causes, three fixes:
1. web_search_20260209 runs a code filter over the results. At effort low the coach's filter code
   iterated the results as dicts when they arrive as a JSON string → return_code=1 → the second
   search hit max_uses (2). Reproduced live from the prod container. The tool type is config now,
   default web_search_20250305 (results land directly; no filter step to break), and every search
   logs its outcome (WEB_SEARCH_RESULT / _EMPTY / _ERROR / _FILTER_ERROR) on every surface,
   the task run included — the failure was invisible in the logs.
2. Wall angels was IN the verified demo library, but the library only reaches context for card
   exercises. An explicit ask for a video now puts the library clips for the moves named (in the ask
   or the coach's recent texts) into context, with the instruction to send them as-is.
3. A lookup task that comes up empty lets the run model write the honest line (nothing_to_report
   takes `message`); code's own fallback never pastes the goal — it names a short topic or nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse, _Response


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    monkeypatch.setattr(config, "DEMO_ASK_CONTEXT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TASK_HONEST_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TASKS_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


# ─── 1. the search tool + its outcome in the logs ────────────────────────────

def test_search_tool_defaults_to_the_version_without_the_code_filter():
    from agent_tools import WEB_SEARCH_TOOL
    assert config.WEB_SEARCH_TOOL_TYPE == "web_search_20250305"
    assert WEB_SEARCH_TOOL["type"] == config.WEB_SEARCH_TOOL_TYPE
    assert WEB_SEARCH_TOOL["name"] == "web_search" and WEB_SEARCH_TOOL["max_uses"] == config.WEB_SEARCH_MAX_USES


def _srv(query):
    return NS(type="server_tool_use", name="web_search", input={"query": query}, id="srvtoolu_1")


def _hits(n):
    return [NS(type="web_search_result", url=f"https://www.youtube.com/watch?v={i}", title=f"clip {i}")
            for i in range(n)]


def _msgs(caplog):
    return [r.getMessage() for r in caplog.records]


def test_results_are_logged_with_their_count(caplog):
    from agent_tools import log_web_search_queries
    content = [_srv("wall angels physical therapist youtube"),
               NS(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=_hits(10))]
    with caplog.at_level(logging.INFO):
        log_web_search_queries(77, content, "agent_loop.run")
    lines = _msgs(caplog)
    assert any("WEB_SEARCH_QUERY user=77" in m for m in lines)
    assert any("WEB_SEARCH_RESULT user=77 site=agent_loop.run n=10" in m for m in lines), lines


def test_an_error_object_and_an_empty_list_are_logged_as_such(caplog):
    from agent_tools import log_web_search_queries
    content = [_srv("q"),
               NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error",
                                                            error_code="max_uses_exceeded")),
               NS(type="web_search_tool_result", content=[])]
    with caplog.at_level(logging.INFO):
        log_web_search_queries(77, content, "tasks.run")
    lines = _msgs(caplog)
    assert any("WEB_SEARCH_ERROR user=77 site=tasks.run code=max_uses_exceeded" in m for m in lines), lines
    assert any("WEB_SEARCH_EMPTY user=77 site=tasks.run" in m for m in lines), lines


def test_a_crashed_filter_step_is_logged(caplog):
    """The live shape (20260209, effort low): the filter code raised on x.get(...) → return_code=1."""
    from agent_tools import log_web_search_queries
    content = [NS(type="server_tool_use", name="code_execution",
                  input={"code": "r=await web_search({...})\nfor x in r:print(x.get('title'))"}),
               _srv("wall angels exercise physical therapist youtube"),
               NS(type="web_search_tool_result", content=_hits(10)),
               NS(type="code_execution_tool_result",
                  content=NS(type="encrypted_code_execution_result", return_code=1, stdout="",
                             stderr="AttributeError: 'str' object has no attribute 'get'"))]
    with caplog.at_level(logging.INFO):
        log_web_search_queries(77, content, "agent_loop.run")
    lines = _msgs(caplog)
    assert any("WEB_SEARCH_FILTER_ERROR user=77" in m and "has no attribute 'get'" in m for m in lines), lines


def test_a_block_without_content_logs_nothing_and_never_raises(caplog):
    from agent_tools import log_web_search_queries
    with caplog.at_level(logging.INFO):
        assert log_web_search_queries(77, [_srv("q"), NS(type="web_search_tool_result")], "x") == ["q"]
    assert not any("WEB_SEARCH_RESULT" in m or "WEB_SEARCH_ERROR" in m for m in _msgs(caplog))


def _task(db, user, goal, **kw):
    from agent_tasks import create_task
    from models import AgentTask
    r = create_task(user.id, goal, delay_minutes=5, **kw)
    row = db.get(AgentTask, r["id"])
    row.run_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    return r["id"]


LIVE_GOAL = ("find short PT/coach reference videos for chin tucks, doorway pec stretch, standing counter "
             "chest stretch, and wall angels. text Alexey one link per move, each on its own line with a "
             "short label")


def test_the_task_run_logs_its_searches_too(db, anthropic_stub, sms_capture, caplog):
    """The 14:21 run searched and failed with no WEB_SEARCH_* line at all — the task loop never logged."""
    from agent_tasks import fire_due
    user = make_user(db, name="Alexey")
    _task(db, user, LIVE_GOAL)
    searched = _Response("")
    searched.content = [_srv("wall angels physical therapist youtube"),
                        NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error",
                                                                     error_code="max_uses_exceeded")),
                        NS(type="tool_use", name="nothing_to_report", input={"reason": "search errored"},
                           id="toolu_n")]
    searched.stop_reason = "tool_use"
    anthropic_stub.push(searched)
    with caplog.at_level(logging.INFO):
        fire_due()
    lines = _msgs(caplog)
    assert any("WEB_SEARCH_QUERY user=" in m and "site=tasks.run" in m for m in lines), lines
    assert any("WEB_SEARCH_ERROR" in m and "site=tasks.run" in m for m in lines), lines


# ─── 2. an explicit video ask gets the library clips in context ──────────────

def _out(db, user, body, minutes_ago, mtype="freeform"):
    from models import Message
    db.add(Message(user_id=user.id, direction="out", body=body, message_type=mtype,
                   created_at=_utcnow() - timedelta(minutes=minutes_ago)))
    db.commit()


def test_new_library_entry_for_the_standing_wall_hinge():
    from exercise_demos import EXERCISE_DEMO_LINKS, _resolve
    assert EXERCISE_DEMO_LINKS["standing_wall_thoracic_extension"] == "https://youtu.be/-TLJEN3Ri3c"
    for phrase in ("wall thoracic extension", "counter thoracic extension", "thoracic extension at the counter"):
        assert _resolve(phrase) == "standing_wall_thoracic_extension", phrase
    assert _resolve("thoracic extension") == "seated_thoracic_extension", "the chair version keeps its alias"
    # Live trial 2026-10-28: the model called a back-to-the-wall arch "standing thoracic extension on the
    # wall". Those generic names are a DIFFERENT movement from the hands-on-wall hinge in the clip.
    for phrase in ("standing thoracic extension", "thoracic extension on the wall"):
        assert _resolve(phrase) is None, phrase


def test_moves_named_in_free_text_are_found_longest_first():
    from exercise_demos import demo_keys_in_text
    t = "do a wall thoracic extension, then wall angels x10. squat later"
    keys = demo_keys_in_text(t)
    assert keys[:2] == ["standing_wall_thoracic_extension", "wall_angel"]
    assert "seated_thoracic_extension" not in keys, "a shorter alias inside a longer match doesn't double-count"
    assert demo_keys_in_text("bench 185 and three plates, no demo words") == []


@pytest.mark.parametrize("ask", ["reference vids pls", "got a video for that?", "any clips?",
                                 "can u send a demo", "show me how", "youtube link?"])
def test_a_video_ask_after_the_coach_named_moves_gets_the_clips(db, ask):
    from agent_loop import _demo_ask_block
    u = make_user(db, name="Alexey")
    _out(db, u, "yeah, a steelcase back just reclines with u so it doesn't do much", 2)
    _out(db, u, "do a wall thoracic extension instead, hold 30s x2. then wall angels x10", 1)
    block = _demo_ask_block(u, ask, db)
    assert block and block.startswith("## DEMO CLIPS ON FILE")
    assert "https://youtu.be/-TLJEN3Ri3c" in block and "https://youtu.be/kcca7_JGsVk" in block
    assert "as-is" in block and "web_search" in block and "never say you couldn't find" in block.lower()
    assert "shows: hands on a wall" in block and "shows: back flat against a wall" in block
    assert "a different version" in block


def test_seen_clips_still_go_out_on_an_explicit_ask_and_get_marked_seen(db, monkeypatch):
    import exercise_demos
    from agent_loop import _demo_ask_block
    marked = []
    monkeypatch.setattr(exercise_demos, "mark_demos_seen", lambda uid, keys: marked.append((uid, list(keys))))
    u = make_user(db, name="Alexey", seen_exercise_demos={"wall_angel": True})
    block = _demo_ask_block(u, "got a video for wall angels?", db)
    assert block and "https://youtu.be/kcca7_JGsVk" in block, "seen only stops UNPROMPTED offers"
    assert marked == [(u.id, ["wall_angel"])]


def test_the_ask_itself_ranks_first_and_the_list_is_capped(db):
    from agent_loop import _demo_ask_block
    u = make_user(db)
    _out(db, u, "chin tucks x10, doorway chest stretch 30s, cat cow x10, dead hangs 30s, hip flexor stretch", 5,
         mtype="reminder")
    block = _demo_ask_block(u, "video for wall angels pls", db)
    lines = [l for l in block.splitlines() if "https://" in l]
    assert lines[0].startswith("wall angel") and len(lines) <= 4


@pytest.mark.parametrize("body", ["how was ur day", "for reference i weigh 165", "send me the link"])
def test_no_ask_or_no_named_move_means_no_block(db, body):
    from agent_loop import _demo_ask_block
    u = make_user(db)
    _out(db, u, "wall angels x10 then chin tucks x10", 1)
    assert _demo_ask_block(u, body, db) is None


def test_old_texts_and_water_pings_dont_count(db):
    from agent_loop import _demo_ask_block
    u = make_user(db)
    _out(db, u, "wall angels x10", 180)
    _out(db, u, "[water-break gif] chin tucks", 1, mtype="water_reminder")
    assert _demo_ask_block(u, "reference vids pls", db) is None


def test_flag_off_is_inert(db, monkeypatch):
    from agent_loop import _demo_ask_block
    monkeypatch.setattr(config, "DEMO_ASK_CONTEXT_ENABLED", False)
    u = make_user(db)
    assert _demo_ask_block(u, "video for wall angels pls", db) is None


def test_the_block_reaches_the_reply_turn(db, anthropic_stub):
    import agent_loop
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db, name="Alexey")
    _out(db, u, "or stand with ur back flat on a wall and do wall angels, x10", 1)
    agent_loop.run_agent_loop(u, "reference vids pls", "freeform")
    system = " ".join((b.get("text", "") if isinstance(b, dict) else str(b))
                      for c in anthropic_stub.calls for b in (c.get("system") or []))
    assert "## DEMO CLIPS ON FILE" in system and "https://youtu.be/kcca7_JGsVk" in system


# ─── 3. the empty-handed / failed task line ──────────────────────────────────

def test_the_run_model_writes_the_empty_handed_line(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due, NOTHING_TO_REPORT_TOOL
    assert "message" in NOTHING_TO_REPORT_TOOL["input_schema"]["properties"]
    user = make_user(db, name="Alexey")
    _task(db, user, LIVE_GOAL)
    anthropic_stub.push(ToolUse("nothing_to_report", {
        "reason": "search errored twice",
        "message": "search kept glitching on me, no clips yet. want me to try again in a bit?"}, id="toolu_n"))
    assert fire_due() == 1
    assert sms_capture[-1][1] == "search kept glitching on me, no clips yet. want me to try again in a bit?"


def test_code_fallback_never_pastes_the_goal(db, anthropic_stub, sms_capture):
    """The live 14:22 line, replayed: no message from the run → code's line, not the goal."""
    from agent_tasks import fire_due
    user = make_user(db, name="Alexey")
    _task(db, user, LIVE_GOAL)
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "search errored"}, id="toolu_n"))
    assert fire_due() == 1
    text = sms_capture[-1][1]
    low = text.lower()
    for leaked in ("find short", "pt/coach", "text alexey", "one link per move", ".."):
        assert leaked not in low, text
    assert "came up empty" in low and "try again" in low and len(text) <= 120


@pytest.mark.parametrize("goal,topic", [
    ("find the CS61C midterm room and text me", "the cs61c midterm room"),
    ("look up the rsf hours for saturday", "the rsf hours for saturday"),
    ("check whether the stats 20 hw got extended", "whether the stats 20 hw got extended"),
])
def test_a_short_goal_becomes_the_topic(db, anthropic_stub, sms_capture, goal, topic):
    from agent_tasks import fire_due
    user = make_user(db, name="Sam")
    _task(db, user, goal)
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "not posted"}, id="toolu_n"))
    assert fire_due() == 1
    low = sms_capture[-1][1].lower()
    assert topic in low and "came up empty" in low, low
    assert "text me" not in low


def test_a_failed_run_says_it_broke_on_our_end(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    from models import AgentTask
    user = make_user(db, name="Alexey")
    tid = _task(db, user, LIVE_GOAL)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(RuntimeError("api down")))
    fire_due()
    row = db.get(AgentTask, tid)
    row.run_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert fire_due() == 1
    low = sms_capture[-1][1].lower()
    assert "on my end" in low and "find short" not in low and "text alexey" not in low


def test_honest_fallback_flag_off_restores_the_old_line(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    monkeypatch.setattr(config, "TASK_HONEST_FALLBACK_ENABLED", False)
    user = make_user(db, name="Sam")
    _task(db, user, "find the CS61C midterm room and text me")
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "x", "message": "model line"}, id="toolu_n"))
    assert fire_due() == 1
    text = sms_capture[-1][1]
    assert text.startswith("couldn't find it") and "find the CS61C midterm room" in text, text


# ─── 4. the coach names the moves it prescribes ──────────────────────────────
# Live 14:14-14:15: the coach DESCRIBED both swaps without naming them ("hands on a wall or counter …
# let ur chest sink", "slide ur arms up and down like a snow angel"), so neither the library nor a
# search query had a name to work from. The voice rule makes it lead with the standard name.

LIVE_UNNAMED = [
    "do it standing instead: hands on a wall or counter at hip height, walk ur feet back, let ur chest "
    "sink toward the floor. hold 30s, x2",
    "or stand with ur back flat on a wall and slide ur arms up and down like a snow angel, x10. either "
    "one counts for the chair part of the pings",
]
NAMED = [
    "wall thoracic extension instead: hands on the wall or a counter at hip height, walk ur feet back, "
    "let ur chest sink. hold 30s, x2",
    "or wall angels: back flat on the wall, slide ur arms up and down like a snow angel, x10",
]


def test_the_live_descriptions_name_nothing_the_library_can_match():
    from exercise_demos import demo_keys_in_text
    assert all(demo_keys_in_text(t) == [] for t in LIVE_UNNAMED)


def test_named_the_way_the_rule_asks_they_resolve_to_the_verified_clips(db):
    from exercise_demos import demo_keys_in_text
    from agent_loop import _demo_ask_block
    assert demo_keys_in_text(NAMED[0]) == ["standing_wall_thoracic_extension"]
    assert demo_keys_in_text(NAMED[1]) == ["wall_angel"]
    u = make_user(db, name="Alexey")
    _out(db, u, NAMED[0], 2)
    _out(db, u, NAMED[1], 1)
    block = _demo_ask_block(u, "reference vids pls", db)
    assert "https://youtu.be/-TLJEN3Ri3c" in block and "https://youtu.be/kcca7_JGsVk" in block


def test_voice_says_name_the_move_then_describe_it():
    import re
    from agent_loop import _voice_prompt
    v = re.sub(r"\s+", " ", _voice_prompt())
    assert "Name every move you prescribe" in v
    assert "the standard name a PT or coach would search" in v
    assert "wall angels" in v and "like a snow angel" in v, "the live example rides with the rule"
    assert "wall thoracic extension: hands on the wall" in v, "an example name comes WITH its how"
    assert "standing thoracic extension on the wall" not in v
    assert "A swap gets a name too" in v


def test_every_posture_clip_says_what_it_shows():
    from exercise_demos import DEMO_CLIP_SHOWS
    for key in ("chin_tuck", "doorway_pec_stretch", "seated_thoracic_extension", "wall_angel", "cat_cow",
                "kneeling_hip_flexor_stretch", "dead_hang", "standing_wall_thoracic_extension"):
        assert DEMO_CLIP_SHOWS.get(key), key


@pytest.mark.parametrize("text", [
    "standing thoracic extension: stand up, hands on ur lower back, squeeze ur shoulder blades and lean back",
    "do a thoracic extension on the wall, back to it, arch off it x10",
])
def test_a_generic_name_for_another_version_maps_to_no_clip(text):
    """Live trial: "standing thoracic extension" (a lean-back) held the chair alias "thoracic extension"
    inside it → the over-the-chair clip. The longer generic name takes the span and maps to nothing."""
    from exercise_demos import demo_keys_in_text
    assert demo_keys_in_text(text) == []

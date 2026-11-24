"""
Two leaks that reached Alex (user 42) in one 48h window (2026-11-18 → 11-20):

1. msg 11305 — reminder id 57 composed by Opus 5.5 at low effort came back as a
   tool call written in XML (an `invoke` element carrying a `parameter name="text"`)
   with stop=end_turn, 56 output tokens — and `reminders._compose` returned it verbatim.
   No prompt names a send_message tool; the model invented the wrapper. The guard is on
   the OUTPUT side: unwrap a lone invoke to its text parameter, fall back when XML is
   mixed into prose, and refuse XML at the one door every outbound text takes.

2. msg 10732 — a CONTINUATION turn: "i already answered this one, so i'm not sending
   anything new.\n\n[silent]". `is_reaction_only_text` matched only the exact sentinel,
   so the narration + sentinel went out as a text. A sentinel on its own line now means
   silence when the rest is the model talking about not sending; a real reply with a
   stray sentinel line is sent without it.

(The XML tags below are assembled from pieces so the file never holds a literal
tool-call element — editors and agents that render those would mangle it.)
"""

from __future__ import annotations

import logging

from tests.factories import make_user
from tests.tier1.test_reactions_and_replies import imessage_on, sidecar, _inbound  # noqa: F401 — fixtures


def _open(tag, attrs="", ns=""):
    return "<" + ns + tag + (" " + attrs if attrs else "") + ">"


def _close(tag, ns=""):
    return "<" + "/" + ns + tag + ">"


def invoke_xml(text, tool="send_message", param="text", ns="", wrap=False):
    body = (_open("invoke", f'name="{tool}"', ns) + "\n"
            + _open("parameter", f'name="{param}"', ns) + text + _close("parameter", ns) + "\n"
            + _close("invoke", ns))
    if wrap:
        body = _open("function_calls", ns=ns) + "\n" + body + "\n" + _close("function_calls", ns)
    return body


ALEX_11305_LINE = "u got time for internship or research apps rn"
ALEX_11305 = invoke_xml(ALEX_11305_LINE)          # byte-for-byte the live body
ALEX_10732 = "i already answered this one, so i'm not sending anything new.\n\n[silent]"


# ── the helper ────────────────────────────────────────────────────────────────

def test_unwrap_leaked_tool_xml_takes_the_text_param_or_refuses():
    from agent_tools import unwrap_leaked_tool_xml, has_leaked_tool_xml
    # The exact live body → the line the model meant to send.
    assert has_leaked_tool_xml(ALEX_11305)
    assert unwrap_leaked_tool_xml(ALEX_11305) == ALEX_11305_LINE
    # Namespaced tags, a function_calls wrapper, other text-ish parameter names: same leak.
    assert unwrap_leaked_tool_xml(invoke_xml("go study, quiz at 4", tool="send_text", param="body",
                                             ns="antml:", wrap=True)) == "go study, quiz at 4"
    assert unwrap_leaked_tool_xml(invoke_xml("legs after lab, ur out at 3", param="message")) == "legs after lab, ur out at 3"
    # Multi-line text inside the parameter survives; surrounding whitespace is trimmed.
    assert unwrap_leaked_tool_xml("  " + invoke_xml("\nline one\nline two\n") + "\n") == "line one\nline two"
    # XML mixed into prose, two invokes, or no text-shaped parameter → None (caller falls back).
    assert unwrap_leaked_tool_xml("ok sending now\n" + ALEX_11305) is None
    assert unwrap_leaked_tool_xml(ALEX_11305 + "\n" + ALEX_11305) is None
    assert unwrap_leaked_tool_xml(invoke_xml("m12", tool="react_to_message", param="message_ref")) is None
    assert unwrap_leaked_tool_xml(invoke_xml("", param="text")) is None
    # Normal text is untouched — including angle brackets that are not tool tags.
    for ok in ("go study, quiz at 4", "bench 3x5 @ 135 <- that's the one", "a <b> tag", "", None):
        assert not has_leaked_tool_xml(ok or ""), ok
        assert unwrap_leaked_tool_xml(ok) == ok


# ── reminders._compose: the site that leaked ─────────────────────────────────

def test_reminder_compose_sends_the_line_inside_the_leaked_invoke(db, anthropic_stub, caplog):
    import reminders
    from models import Reminder
    user = make_user(db, name="Alex")
    row = Reminder(user_id=user.id, text="hey check if u have time for internship or research applications",
                   local_time="17:30", recur_days="fri")
    anthropic_stub.reply_with(lambda kw: ALEX_11305)
    with caplog.at_level(logging.INFO):
        assert reminders._compose(user, row) == ALEX_11305_LINE
    assert any("REMINDER_COMPOSE_TOOL_XML" in r.getMessage() for r in caplog.records)


def test_reminder_compose_falls_back_when_xml_is_mixed_into_prose(db, anthropic_stub, caplog):
    import reminders
    from models import Reminder
    user = make_user(db, name="Alex")
    row = Reminder(user_id=user.id, text="go run", local_time="19:30", recur_days="tue,thu")
    anthropic_stub.reply_with(lambda kw: "sending it now:\n" + ALEX_11305)
    with caplog.at_level(logging.WARNING):
        assert reminders._compose(user, row) == "reminder: go run"
    assert any("REMINDER_COMPOSE_TOOL_XML" in r.getMessage() for r in caplog.records)


def test_reminder_compose_plain_line_is_untouched(db, anthropic_stub):
    import reminders
    from models import Reminder
    user = make_user(db, name="Alex")
    anthropic_stub.reply_with(lambda kw: "run time, go now")
    assert reminders._compose(user, Reminder(user_id=user.id, text="go run")) == "run time, go now"


# ── the other one-line composers that return raw model text ───────────────────

def test_workout_review_and_reengage_copy_unwrap_or_refuse_tool_xml(db, anthropic_stub):
    from workouts.review import _call
    user = make_user(db, name="Alex")
    anthropic_stub.reply_with(lambda kw: invoke_xml("solid session. bench moved up"))
    assert _call(user.id, "workout.plan", [{"type": "text", "text": "sys"}], "go") == "solid session. bench moved up"
    anthropic_stub.reply_with(lambda kw: "here:\n" + invoke_xml("solid session"))
    assert _call(user.id, "workout.plan", [{"type": "text", "text": "sys"}], "go") is None


def test_heartbeat_bare_text_fallback_unwraps_tool_xml(monkeypatch):
    """The heartbeat's 'model ended with bare text instead of send_text' seam."""
    import heartbeat
    from tests._fake_anthropic import _Response
    seen = {}
    monkeypatch.setattr(heartbeat, "_enforce_briefing", lambda text, briefing, uid: seen.setdefault("text", text))
    monkeypatch.setattr(heartbeat, "_guard_schedule_claims", lambda text, uid: text)
    assert heartbeat._bare_text_fallback(_Response(invoke_xml("how's studying going")).content, 42) == "how's studying going"
    assert heartbeat._bare_text_fallback(_Response("hey:\n" + invoke_xml("x")).content, 42) is None
    assert heartbeat._bare_text_fallback(_Response("how's studying going").content, 42) == "how's studying going"


# ── the one door every outbound takes ─────────────────────────────────────────

def test_send_sms_unwraps_a_lone_invoke_and_refuses_mixed_xml(db, sms_capture, caplog):
    import sms
    user = make_user(db, preferred_channel="sms")
    with caplog.at_level(logging.WARNING):
        sms.send_sms(user.phone, ALEX_11305, user_id=user.id, message_type="reminder")
        # (a different line, so the near-duplicate suppressor doesn't swallow this one first)
        sms.send_sms(user.phone, "sending:\n" + invoke_xml("run time, go now"), user_id=user.id, message_type="reminder")
    assert [b for _, b in sms_capture] == [ALEX_11305_LINE]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("SEND_TOOL_XML_UNWRAPPED" in m for m in msgs) and any("SEND_BLOCKED_TOOL_XML" in m for m in msgs)
    # The blocked one never became an outbound row either.
    from models import Message
    db.expire_all()
    bodies = [m.body for m in db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")]
    assert bodies == [ALEX_11305_LINE]


# ── the [silent] trailer ──────────────────────────────────────────────────────

def test_sentinel_on_its_own_line_is_silence_when_the_rest_is_about_not_sending():
    from agent_tools import is_reaction_only_text, strip_sentinel_line, REACTION_ONLY_SENTINEL
    assert is_reaction_only_text(ALEX_10732, False)                       # the live leak
    assert is_reaction_only_text("[silent]\n\nalready covered above", False)
    assert is_reaction_only_text("the tapback was the reply.\n[silent]", True)
    assert is_reaction_only_text(REACTION_ONLY_SENTINEL, False)           # unchanged
    assert not is_reaction_only_text("go study, quiz at 4", True)         # unchanged
    assert not is_reaction_only_text("No text needed.", False)            # unchanged: no reaction, no sentinel
    # A real reply with a stray sentinel line is NOT silence — it is sent without the line.
    assert not is_reaction_only_text("logged it, 2105 cal on the day\n\n[silent]", False)
    assert strip_sentinel_line("logged it, 2105 cal on the day\n\n[silent]") == "logged it, 2105 cal on the day"
    assert strip_sentinel_line("[silent]\nlogged it") == "logged it"
    assert strip_sentinel_line("go study, quiz at 4") == "go study, quiz at 4"
    assert strip_sentinel_line("silent treatment lol, [silent] isn't a word") == "silent treatment lol, [silent] isn't a word"


def test_alex_10732_turn_sends_nothing_and_backfills_the_thumbs_up(db, imessage_on, sidecar, anthropic_stub, sms_capture, monkeypatch):
    import app
    import config
    monkeypatch.setattr(config, "SILENT_SENTINEL_BACKFILL_ENABLED", True)
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "And more protein", "spc-msg-protein")
    anthropic_stub.reply_with(lambda kw: ALEX_10732)
    app.process_buffered_message(user.id, "And more protein", "freeform")
    routes = [r for r, _ in sidecar]
    assert "send" not in routes and sms_capture == [], routes
    assert routes.count("react") == 1


def test_real_reply_with_a_stray_sentinel_line_goes_out_without_it(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "ima eat 15 grapes", "spc-msg-grapes")
    anthropic_stub.reply_with(lambda kw: "logged the grapes, 2105 cal on the day\n\n[silent]")
    app.process_buffered_message(user.id, "ima eat 15 grapes", "freeform")
    sends = [j["text"] for r, j in sidecar if r == "send"]
    assert sends == ["logged the grapes, 2105 cal on the day"], sends


# ── the agent loop's own text path ────────────────────────────────────────────

def test_agent_loop_lone_invoke_text_is_sent_as_its_line(db, imessage_on, sidecar, anthropic_stub, sms_capture, caplog):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "what should i eat", "spc-msg-eat")
    anthropic_stub.reply_with(lambda kw: invoke_xml("chorizo and egg tacos, 3 eggs"))
    with caplog.at_level(logging.WARNING):
        app.process_buffered_message(user.id, "what should i eat", "freeform")
    sends = [j["text"] for r, j in sidecar if r == "send"]
    assert sends == ["chorizo and egg tacos, 3 eggs"], sends
    assert any("AGENT_LOOP_TOOL_XML_IN_TEXT" in r.getMessage() for r in caplog.records)


def test_agent_loop_xml_mixed_into_prose_is_nudged_then_the_real_reply_goes(db, imessage_on, sidecar, anthropic_stub, sms_capture, caplog):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "what should i eat", "spc-msg-eat2")
    anthropic_stub.push("ok let me send that\n" + invoke_xml("chorizo and egg tacos"), "chorizo and egg tacos")
    with caplog.at_level(logging.WARNING):
        app.process_buffered_message(user.id, "what should i eat", "freeform")
    sends = [j["text"] for r, j in sidecar if r == "send"]
    assert sends == ["chorizo and egg tacos"], sends
    assert any("AGENT_LOOP_NARRATION_NUDGE" in r.getMessage() for r in caplog.records)

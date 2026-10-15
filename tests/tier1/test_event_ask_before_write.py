"""Live 2026-10-15 (user 51): "my midterm is at 7pm today" while the calendar said Friday → the
coach asked "wait midterm's today at 7 not tmrw?" AND logged the event in the same turn; the
reply "Nah not today" (about the gym) was read as the midterm correction, and the radar kept
both days. The tool now says: a conflicting date → ask first, log after; a corrected date → fix
the row in the same turn."""


def test_log_event_tool_tells_the_coach_to_ask_before_writing_a_conflicting_date():
    from agent_tools import LOG_EVENT_TOOL
    d = LOG_EVENT_TOOL["description"]
    assert "ask which is right FIRST and do not log it in the same turn you question it" in d
    assert "fix that row with manage_log (edit or delete) in the same turn" in d

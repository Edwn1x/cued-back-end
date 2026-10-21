"""Live 2026-10-21 12:59 PT: BRIEFING_DROPPED_ITEM item=due_today detail='CS 70 HW Due' on a
briefing that (per the matcher's own docstring) should match 'CS 70 hw'. Fusing both
letter→digit and digit→letter gaps at once turned "cs 70 hw" into "cs70hw", so the fused
candidate "cs70" never sat on a boundary. Each fusion direction is now tried on its own."""
import pytest


@pytest.mark.parametrize("text", [
    "cs70 hw due at 4",
    "cs 70 hw due at 4 today",
    "CS 70 HW due today at 4pm, then discussion",
    "cs70 hw lands wed",
    "the cs 70 homework is due at 4",
])
def test_spaced_and_fused_course_codes_both_match(text):
    import heartbeat
    assert heartbeat._mentions_title(text, "CS 70 HW Due"), text


@pytest.mark.parametrize("text", ["hw due today at 4", "nothing due today", "61c lecture at 11"])
def test_generic_paraphrases_still_count_as_drops(text):
    import heartbeat
    assert not heartbeat._mentions_title(text, "CS 70 HW Due"), text

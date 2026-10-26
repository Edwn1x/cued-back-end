"""
Live 2026-10-25 18:38–18:41 PT, user 51 (the founder):

  in : I'm seriously considering hopping on a cycle / But idk what, trt?
  out: nah don't / ur 20 and 142. ur nowhere near your natural ceiling. a cycle now just caps
       what u could've built and messes with ur own test production, sometimes for good
       / what's driving it, feeling stuck?
  in : Nah just if you want extraordinary results you have to take extraordinary actions
  out: [👍]      ← AGENT_LOOP_RESTATEMENT_SUPPRESSED figure=142
                   reply='extraordinary action is eating enough and training hard for 2 yrs straight. u ha…'
  in : So trt is the entry level or what?

The composed pushback re-cited his bodyweight ("ur 142"); the day-total regex read that as a
total already stated and the guard turned a steroid pushback into a thumbs-up — which read as
agreement. Two gates fix the premise without loosening the live pair the guard exists for:
a substantive inbound (a real sentence, not an ack) earns a real reply; a reply with many words
the previous send never used is new content, whatever figure it repeats.
"""
import pytest

PREV = ("nah don't\nur 20 and 142. ur nowhere near your natural ceiling. a cycle now just caps what u "
        "could've built and messes with ur own test production, sometimes for good\nwhat's driving it, feeling stuck?")
INBOUND = "Nah just if you want extraordinary results you have to take extraordinary actions"
REPLY = ("extraordinary action is eating enough and training hard for 2 yrs straight. u have a ton of "
         "natural muscle left to build — ur 142 at 20, that's the extraordinary part, almost nobody does it")

# the guard's own live pair — must still be caught
PREV_REPLY = "cut it to 450, banana's in\n555 cal, 20g protein so far"
RESTATEMENT = "That banana's already in — logged it at 105 cal. Ur at 555 for the day."


def test_live_steroid_pushback_is_not_a_restatement(monkeypatch):
    import config
    from agent_loop import _restatement_figure
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_NOVELTY_GATE_ENABLED", True, raising=False)
    assert _restatement_figure(REPLY, INBOUND, PREV) is None


def test_a_substantive_inbound_alone_earns_a_real_reply(monkeypatch):
    import config
    from agent_loop import _restatement_figure
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_NOVELTY_GATE_ENABLED", True, raising=False)
    # even a reply that re-says the total verbatim passes when the inbound is a real sentence
    assert _restatement_figure(RESTATEMENT, "i honestly think the banana should count as more than that", PREV_REPLY) is None


# ("got it" is excluded: the pre-existing _INBOUND_QUESTION_RE treats a leading "got" as a question)
@pytest.mark.parametrize("ack", ["ok", "bet", "nah", "thanks", "lol", "👍", "sounds good", "alr", "yeah"])
def test_an_ack_inbound_still_lets_the_live_pair_be_caught(ack, monkeypatch):
    import config
    from agent_loop import _restatement_figure
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_NOVELTY_GATE_ENABLED", True, raising=False)
    assert _restatement_figure(RESTATEMENT, ack, PREV_REPLY) == 555


def test_novel_words_measure_and_threshold(monkeypatch):
    import config
    from agent_loop import _novel_words, _restatement_figure
    assert _novel_words(RESTATEMENT, PREV_REPLY) < config.OUTBOUND_RESTATEMENT_NOVEL_WORDS
    assert _novel_words(REPLY, PREV) >= config.OUTBOUND_RESTATEMENT_NOVEL_WORDS
    # a short re-say that repeats the bodyweight with nothing new IS still a restatement
    assert _restatement_figure("yeah ur 142, nowhere near ur ceiling", "", PREV) == 142


def test_flag_off_restores_the_live_miss(monkeypatch):
    import config
    from agent_loop import _restatement_figure
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_NOVELTY_GATE_ENABLED", False, raising=False)
    assert _restatement_figure(REPLY, INBOUND, PREV) == 142


def test_flags_default():
    import config
    assert config.OUTBOUND_RESTATEMENT_NOVELTY_GATE_ENABLED is True
    assert config.OUTBOUND_RESTATEMENT_NOVEL_WORDS == 10 and config.OUTBOUND_RESTATEMENT_SUBSTANTIVE_WORDS == 6

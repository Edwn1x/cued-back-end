"""
Opus 5.5 follow-up (2026-10-27): the coaching-summary job read response.content[0].text.
On Opus 5.5 content[0] is a THINKING block (no .text), so the read raised, the write
never ran, the watermark never advanced, and the job re-ran at full cost on every
reply (user 77: 27 runs in one afternoon). Text is now read by block type; an empty
result keeps the prior summary and watermark; small-cap Opus sites set effort low.
"""

from __future__ import annotations


def _seed_messages(user_id, n):
    from models import get_session, Message
    s = get_session()
    try:
        for i in range(n):
            s.add(Message(user_id=user_id, direction="in" if i % 2 == 0 else "out",
                          body=f"msg {i}", message_type="freeform"))
        s.commit()
    finally:
        s.close()


def _user(user_id):
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        return u.coaching_summary, u.last_compressed_message_id
    finally:
        s.close()


def test_summary_written_when_first_block_is_thinking(db, anthropic_stub):
    import app
    from tests._fake_anthropic import MultiText
    from tests.factories import make_user
    user = make_user(db)
    _seed_messages(user.id, 20)
    seen = {}

    def handler(kw):
        seen["kw"] = kw
        # Opus 5.5 shape: thinking block first (empty under display=omitted), then text
        return MultiText(("thinking", ""), "## Recent Themes\n- folded")
    anthropic_stub.reply_with(handler)

    app.maybe_update_coaching_summary(user.id)

    summary, wm = _user(user.id)
    assert summary == "## Recent Themes\n- folded"
    assert wm is not None and wm > 0, "watermark must advance after a successful fold"
    assert seen["kw"]["thinking"] == {"type": "adaptive"}
    assert seen["kw"]["output_config"] == {"effort": "low"}
    assert seen["kw"]["max_tokens"] >= 3000


def test_summary_does_not_rerun_once_folded(db, anthropic_stub):
    import app
    from tests._fake_anthropic import MultiText
    from tests.factories import make_user
    user = make_user(db)
    _seed_messages(user.id, 20)
    anthropic_stub.reply_with(lambda kw: MultiText(("thinking", ""), "summary v1"))
    app.maybe_update_coaching_summary(user.id)
    app.maybe_update_coaching_summary(user.id)
    assert len(anthropic_stub.calls) == 1, "once the watermark advances, the next turn has <16 new → no call"


def test_empty_text_keeps_prior_summary_and_watermark(db, anthropic_stub, caplog):
    import app
    from tests._fake_anthropic import MultiText
    from tests.factories import make_user
    user = make_user(db, coaching_summary="prior", last_compressed_message_id=None)
    _seed_messages(user.id, 20)
    anthropic_stub.reply_with(lambda kw: MultiText(("thinking", "")))
    with caplog.at_level("WARNING"):
        app.maybe_update_coaching_summary(user.id)
    summary, wm = _user(user.id)
    assert summary == "prior" and wm is None
    assert "BG_JOB_EMPTY" in caplog.text


def test_small_cap_opus_sites_set_low_effort(db, anthropic_stub):
    """reminders / re-engage / workout review: thinking is always on now, so each
    small-output site must pin effort low and leave headroom in max_tokens."""
    import reminders
    from models import Reminder
    from tests.factories import make_user
    user = make_user(db, name="Alex")
    seen = []
    anthropic_stub.reply_with(lambda kw: (seen.append(kw) or "water time"))
    reminders._compose(user, Reminder(user_id=user.id, text="drink water", every_hours=2))
    assert seen and seen[-1]["output_config"] == {"effort": "low"} and seen[-1]["max_tokens"] >= 600

    from workouts.review import _call
    _call(user.id, "workout.plan", [{"type": "text", "text": "sys"}], "go")
    assert seen[-1]["output_config"] == {"effort": "low"} and seen[-1]["max_tokens"] >= 1200

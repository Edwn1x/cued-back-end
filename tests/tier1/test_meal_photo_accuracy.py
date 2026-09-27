"""
Meal / vision logging accuracy bundle (the 2026-09-26 incidents). Tier-1 pins the
deterministic halves; the model's actual compliance (does it log BOTH foods, does it
refine on a named hall) is tier-2.

Covered here:
  1. text+photo different foods → the log_meal path lets both be logged (no structural
     one-food-per-message assumption) + a voice tripwire.
  2. no silent delete of a confirmed meal on a photo re-read (manage_log guard).
  3. cross-turn image persistence (recent_media): a photo turn leaves a durable note
     that survives the conversation window rolling past it.
  4. inline ￼ (U+FFFC) failed-image guard: a placeholder-only message never reaches the
     model as describable content.
  5. named-dining-hall post-log refine: match_dining_item on an already-logged item
     surfaces the row + arms the write-back nudge.
  6. proactive-claims log-check: a voice tripwire (today's totals already ride the
     proactive context via build_loop_context).
"""

from __future__ import annotations

import config

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}


# ── shared helpers ───────────────────────────────────────────────────────────

def _fresh_context(user_id):
    from models import get_session, User
    from agent_loop import build_loop_context
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def _user_row(user_id):
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        return dict(recent_photos=u.recent_photos)
    finally:
        s.close()


# ── 1. text+photo different foods = log both (structural: no one-food assumption) ──

def test_log_meal_logs_both_text_and_photo_foods_as_separate_items(db, monkeypatch, anthropic_stub):
    """The furious incident: 'ate 2 bananas' + a photo of 2 yogurt cups. The model must be
    able to log BOTH. Structurally log_meal takes an items list (or repeat calls); nothing
    drops the second food. Here the model logs both in one items call."""
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)

    anthropic_stub.push(
        ToolUse("log_meal", {"items": [
            {"description": "2 bananas", "calories": 210, "protein_g": 2, "carbs_g": 54, "fat_g": 1},
            {"description": "2 Chobani peach yogurt cups", "calories": 240, "protein_g": 24, "carbs_g": 26, "fat_g": 4},
        ]}),
        "logged both — bananas + the yogurt cups",
    )
    user = make_user(db)
    run_agent_loop(user, "ate 2 bananas", "food_photo", image_data=_IMG)

    s = get_session()
    try:
        descs = sorted(m.description for m in active(s, Meal, user_id=user.id).all())
    finally:
        s.close()
    assert len(descs) == 2, f"both the caption food and the photo food must be logged: {descs}"
    assert any("banana" in d for d in descs) and any("yogurt" in d for d in descs)


def test_voice_has_the_log_both_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "log BOTH, as separate items" in v
    assert "never just the one you noticed first" in v


# ── 2. no silent delete of a confirmed entry on a photo re-read ───────────────

def _log_a_meal(user_id, desc="2 bananas", cal=210):
    from agent_tools import handle_log_meal
    return handle_log_meal(user_id, {"description": desc, "calories": cal,
                                     "protein_g": 2, "carbs_g": 54, "fat_g": 1})


def test_photo_reread_delete_of_a_meal_is_blocked_without_intent(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_manage_log, begin_turn, peek_turn_state
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    user = make_user(db)
    _log_a_meal(user.id)
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).one().id
    finally:
        s.close()

    # A photo turn with no delete words in the caption — the yogurt-photo case.
    begin_turn(user.id)
    st = peek_turn_state(user.id)
    st["has_image"] = True
    st["caption"] = "yogurt"           # a new food, not a delete request

    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": mid})
    assert out.startswith("error:"), out
    assert "SEPARATE entry" in out or "ADDS food" in out
    s = get_session()
    try:
        assert active(s, Meal, user_id=user.id).count() == 1, "the confirmed meal must NOT be deleted"
    finally:
        s.close()


def test_photo_reread_delete_allowed_with_explicit_intent(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_manage_log, begin_turn, peek_turn_state
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    user = make_user(db)
    _log_a_meal(user.id)
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).one().id
    finally:
        s.close()

    begin_turn(user.id)
    st = peek_turn_state(user.id)
    st["has_image"] = True
    st["caption"] = "delete that, i didn't eat the bananas"

    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": mid})
    assert out.startswith("ok:"), out
    s = get_session()
    try:
        assert active(s, Meal, user_id=user.id).count() == 0
    finally:
        s.close()


def test_text_turn_delete_is_never_blocked(db, monkeypatch):
    """No image on the turn → the guard is inert; a plain text delete still works."""
    from tests.factories import make_user
    from agent_tools import handle_manage_log, begin_turn, peek_turn_state
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    user = make_user(db)
    _log_a_meal(user.id)
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).one().id
    finally:
        s.close()

    begin_turn(user.id)                # has_image defaults False
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": mid})
    assert out.startswith("ok:"), out


def test_voice_has_the_no_photo_delete_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "A new photo never deletes a prior confirmed entry" in v


# ── 3. cross-turn image persistence (recent_media) ───────────────────────────

def test_apply_photo_caps_and_drops_empty():
    from recent_media import apply_photo
    assert apply_photo(None, "", "") == []               # nothing to reference
    rec = apply_photo(None, "cap", "saw a chicken bowl")
    assert len(rec) == 1 and rec[0]["caption"] == "cap"
    # cap at RECENT_MEDIA_MAX
    for i in range(config.RECENT_MEDIA_MAX + 3):
        rec = apply_photo(rec, f"c{i}", f"s{i}")
    assert len(rec) == config.RECENT_MEDIA_MAX
    assert rec[-1]["summary"] == f"s{config.RECENT_MEDIA_MAX + 2}"   # newest kept


def test_photo_turn_persists_a_recent_note_that_survives_window_loss(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Message

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "RECENT_MEDIA_ENABLED", True)

    anthropic_stub.push("that's a solid chicken bowl, logged it ~650 cal")
    user = make_user(db)
    run_agent_loop(user, "lunch", "food_photo", image_data=_IMG)

    assert _user_row(user.id)["recent_photos"], "an image turn must leave a durable photo note"

    # window rolls entirely past the turn
    s = get_session()
    try:
        s.query(Message).filter(Message.user_id == user.id).delete()
        s.commit()
    finally:
        s.close()

    ctx = _fresh_context(user.id)
    assert "RECENT PHOTOS THEY SENT" in ctx
    assert "chicken bowl" in ctx, "the stored read must survive the window loss"


def test_text_only_turn_persists_no_photo(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    anthropic_stub.push("sounds good")
    user = make_user(db)
    run_agent_loop(user, "hey what's up", "freeform")
    assert not _user_row(user.id)["recent_photos"]


def test_stale_photos_age_out_of_context():
    from recent_media import render_recent_photos_block
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc).replace(tzinfo=None)
           - timedelta(hours=config.RECENT_MEDIA_TTL_HOURS + 1)).isoformat()
    stale = [{"at": old, "caption": "x", "summary": "old thing"}]
    assert render_recent_photos_block(stale) == ""


# ── 4. inline ￼ (U+FFFC) failed-image guard ──────────────────────────────────

def test_failed_image_text_detector():
    from agent_loop import _is_failed_image_text
    assert _is_failed_image_text("￼")
    assert _is_failed_image_text("￼￼￼")
    assert _is_failed_image_text("  ￼ \n")
    assert not _is_failed_image_text("￼ 2 bananas")   # has real words → NOT a bare placeholder
    assert not _is_failed_image_text("hello")
    assert not _is_failed_image_text("")


def test_placeholder_only_message_never_reaches_model_as_describable(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "INLINE_IMAGE_PLACEHOLDER_GUARD_ENABLED", True)
    anthropic_stub.push("looks like the pic didn't come through — mind resending?")
    user = make_user(db)
    run_agent_loop(user, "￼", "freeform")

    sent = anthropic_stub.calls[-1]["messages"][0]["content"]
    assert isinstance(sent, str)
    assert "didn't come through" in sent
    assert "NOT describe or guess" in sent
    assert "￼" not in sent, "the raw placeholder must be replaced, not forwarded"


def test_voice_has_the_failed_image_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "A `￼`-only message is a failed send, not food" in v


# ── 5. named-dining-hall post-log macro refine ───────────────────────────────

def _add_dining_item(db, name="Roasted Garlic Halal Chicken Rice Bowl"):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from models import DiningMenuItem
    today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    row = DiningMenuItem(scraped_date=today, hall="crossroads", meal_period="lunch",
                         station="entrees", item_name=name, calories=720, protein_g=42.0,
                         carbs_g=68.0, fat_g=24.0, serving_size="1 bowl")
    db.add(row)
    db.commit()
    return row


def test_named_hall_match_surfaces_logged_row_and_arms_writeback(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_match_dining_item, begin_turn, peek_turn_state

    monkeypatch.setattr(config, "DINING_MATCH_TOOL_ENABLED", True)
    _add_dining_item(db)
    user = make_user(db)
    _log_a_meal(user.id, desc="halal chicken bowl", cal=650)

    begin_turn(user.id)
    out = handle_match_dining_item(user.id, {"description": "halal chicken bowl", "hall": "crossroads"})
    assert out.startswith("ok: menu matches:")
    assert "already logged" in out, "a named-hall match on an existing entry must flag the refine"
    assert peek_turn_state(user.id).get("pending_writeback"), "the write-back nudge must be armed"


def test_voice_has_the_named_venue_refine_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "A named venue on an already-logged meal is a refine, not a shrug" in v


# ── 6. proactive-claims log-check ────────────────────────────────────────────

def test_voice_has_the_proactive_claim_logcheck_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "governs your PROACTIVE CLAIMS too" in v
    assert "without READING TODAY'S TOTALS first" in v


def test_todays_totals_ride_the_proactive_context(db):
    """Fix #6's structural half: the heartbeat/proactive context is built on
    build_loop_context, which ALWAYS renders TODAY'S TOTALS (even at 0), so the model
    can never claim 'no food' without the number being right there."""
    from tests.factories import make_user
    user = make_user(db)
    ctx = _fresh_context(user.id)
    assert "TODAY'S TOTALS" in ctx

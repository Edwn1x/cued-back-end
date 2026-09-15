"""
Series §1 — receipts → pantry. Classifier (meal never hits extraction), a Trader
Joe's fixture → the right three items and the right weekday for a fixed target,
no 'stocked through' without a target, each depletion phrase, the inventory
line, and the PANTRY context block. All model/USDA calls are stubbed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/AAAA"}}

TJ_RECEIPT = {
    "store": "trader joe's",
    "items": [
        {"name": "greek yogurt plain 32oz", "qty": 1, "unit": "each", "price": 4.49, "is_food": True, "est_grams": 907},
        {"name": "eggs large dozen", "qty": 1, "unit": "dozen", "price": 3.99, "is_food": True, "est_grams": 600},
        {"name": "chicken thighs boneless", "qty": 2.1, "unit": "lb", "price": 9.77, "is_food": True, "est_grams": 950},
        {"name": "paper bag", "qty": 1, "unit": "each", "price": 0.25, "is_food": False, "est_grams": 0},
        {"name": "bananas", "qty": 6, "unit": "each", "price": 1.14, "is_food": True, "est_grams": 700},
        {"name": "tax", "qty": 1, "unit": None, "price": 0.0, "is_food": False, "est_grams": 0},
    ],
}
USDA = {  # per 100 g protein
    "greek yogurt plain 32oz": ("yogurt, greek, plain, nonfat", 10.2),
    "eggs large dozen": ("egg, whole, raw, fresh", 12.6),
    "chicken thighs boneless": ("chicken, thigh, boneless, skinless, raw", 19.7),
    "bananas": ("bananas, raw", 1.1),
}


@pytest.fixture
def receipts_on(monkeypatch):
    import config, receipts
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", True)
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(receipts, "canonicalize", lambda name: USDA.get(name, (name, None)))


def _user(db, **kw):
    base = dict(name="Nau", onboarding_step=3, protein_target=139, user_timezone="America/Los_Angeles",
                cooking_situation="cooks")
    base.update(kw)
    return make_user(db, **base)


# ─── classify + extract are stubbed at the module seam ──────────────────────

def test_receipt_photo_is_itemized_and_answered_in_code(db, receipts_on, monkeypatch):
    import receipts
    from models import get_session, PantryItem, Signal
    user = _user(db)
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "receipt")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: TJ_RECEIPT)
    reply = receipts.handle_receipt_image(user.id, IMG)
    # protein on hand: yogurt 907*.102=92.5, eggs 600*.126=75.6, chicken 950*.197=187.2, bananas 7.7 → 363 g
    # 363 // 139 = 2 days → today + 2 (local)
    tz = ZoneInfo("America/Los_Angeles")
    through = (datetime.now(tz).date() + timedelta(days=2)).strftime("%A").lower()
    assert reply == f"got your trader joe's receipt. logged chicken thighs boneless, greek yogurt plain 32oz, and eggs large dozen — you're stocked through {through}."
    s = get_session()
    try:
        rows = s.query(PantryItem).filter_by(user_id=user.id).order_by(PantryItem.id).all()
        assert [r.item for r in rows] == ["yogurt, greek, plain, nonfat", "egg, whole, raw, fresh",
                                          "chicken, thigh, boneless, skinless, raw", "bananas, raw"]   # bag + tax skipped
        assert rows[2].protein_per_100g == 19.7 and rows[2].est_grams == 950 and rows[2].source == "receipt"
        sig = s.query(Signal).filter_by(user_id=user.id, kind="receipt").one()
        assert sig.payload["store"] == "trader joe's" and len(sig.payload["items"]) == 4
    finally:
        s.close()


def test_stocked_through_is_capped_and_dropped_without_a_target(db, receipts_on, monkeypatch):
    import receipts
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "receipt")
    big = dict(TJ_RECEIPT, items=[{"name": "chicken thighs boneless", "qty": 20, "unit": "lb", "is_food": True, "est_grams": 9000}])
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: big)
    tz = ZoneInfo("America/Los_Angeles")
    cap = (datetime.now(tz).date() + timedelta(days=7)).strftime("%A").lower()
    assert receipts.handle_receipt_image(_user(db).id, IMG).endswith(f"stocked through {cap}.")   # 1773 g / 139 → capped at 7
    reply = receipts.handle_receipt_image(_user(db, protein_target=None).id, IMG)
    assert reply == "got your trader joe's receipt. logged chicken thighs boneless."         # no guess without a target


def test_meal_photo_never_hits_extraction(db, receipts_on, monkeypatch):
    import receipts
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "meal")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: (_ for _ in ()).throw(AssertionError("must not extract a meal")))
    assert receipts.handle_receipt_image(_user(db).id, IMG) is None
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "other")
    assert receipts.handle_receipt_image(_user(db).id, IMG) is None


def test_flag_off_means_no_classifier_call(db, monkeypatch):
    import config, receipts
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", False)
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: (_ for _ in ()).throw(AssertionError("flag off")))
    assert receipts.handle_receipt_image(_user(db).id, IMG) is None


def test_loop_returns_the_receipt_reply_without_a_model_turn(db, receipts_on, monkeypatch, anthropic_stub, sms_capture):
    import receipts
    from agent_loop import run_agent_loop
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "receipt")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: TJ_RECEIPT)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("the coach model must not run on a receipt")))
    user = _user(db)
    reply = run_agent_loop(user, "", "food_photo", image_data=IMG)
    assert reply.startswith("got your trader joe's receipt. logged chicken thighs boneless")


def test_classifier_and_extractor_parse_model_output(db, receipts_on, anthropic_stub):
    """The real seams: a one-word classification (with a trailing period) and JSON in
    fences; a failed classify degrades to 'meal' (the photo still gets handled)."""
    import receipts
    calls = []

    def handler(kw):
        calls.append(kw)
        text = json.dumps(kw["messages"][0]["content"])
        if "Classify this photo" in text:
            return "Receipt."
        return "```json\n" + json.dumps(TJ_RECEIPT) + "\n```"
    anthropic_stub.reply_with(handler)
    assert receipts.classify_image(IMG, 1) == "receipt"
    assert receipts.extract_receipt(IMG, 1)["store"] == "trader joe's"
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(RuntimeError("api down")))
    assert receipts.classify_image(IMG, 1) == "meal"
    assert receipts.extract_receipt(IMG, 1) is None


# ─── 1.4 text ───────────────────────────────────────────────────────────────

def _stock(db, user):
    import receipts
    receipts.ingest_receipt(user.id, TJ_RECEIPT)


@pytest.mark.parametrize("phrase,label", [
    ("finished the eggs", "eggs large dozen"),
    ("out of chicken", "chicken thighs boneless"),
    ("ate the last of the yogurt", "greek yogurt plain 32oz"),
    ("we're out of bananas", "bananas"),
    ("used up the chicken thighs", "chicken thighs boneless"),
])
def test_each_depletion_phrase(db, receipts_on, phrase, label):
    import receipts
    from models import get_session, PantryItem
    user = _user(db); _stock(db, user)
    assert receipts.handle_pantry_text(user.id, phrase) == f"noted — {label} is off the list."
    s = get_session()
    try:
        row = s.query(PantryItem).filter_by(user_id=user.id, label=label).one()
        assert row.depleted_at is not None
        assert s.query(PantryItem).filter_by(user_id=user.id).filter(PantryItem.depleted_at.is_(None)).count() == 3
    finally:
        s.close()


def test_depletion_of_something_not_in_the_pantry_is_a_normal_turn(db, receipts_on):
    import receipts
    user = _user(db); _stock(db, user)
    assert receipts.handle_pantry_text(user.id, "out of patience") is None
    assert receipts.handle_pantry_text(user.id, "what should i eat") is None


def test_inventory_line_formatting(db, receipts_on):
    import receipts
    user = _user(db)
    assert receipts.handle_pantry_text(user.id, "what do i have") == "nothing in the pantry yet — send me a receipt or tell me what you've got."
    _stock(db, user)
    line = receipts.handle_pantry_text(user.id, "what's in the fridge?")
    assert line == "chicken thighs boneless (~2.1 lb), greek yogurt plain 32oz (1), eggs large dozen (1), bananas (6). low on everything else."
    receipts.handle_pantry_text(user.id, "out of bananas")
    assert receipts.handle_pantry_text(user.id, "whats in my fridge").endswith("eggs large dozen (1). low on everything else.")


def test_pantry_text_runs_before_the_model_in_the_pipeline(db, receipts_on, driver, anthropic_stub, sms_capture):
    user = _user(db); _stock(db, user)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run for a pantry text")))
    driver.send(user, "out of chicken")
    assert sms_capture[-1][1] == "noted - chicken thighs boneless is off the list."   # GSM-7 dash


# ─── 1.5 context ────────────────────────────────────────────────────────────

def test_pantry_context_block_lists_active_items_only(db, receipts_on):
    import receipts
    from agent_loop import build_loop_context
    from models import get_session, User
    user = _user(db); _stock(db, user)
    receipts.handle_pantry_text(user.id, "finished the eggs")
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## PANTRY (what they have at home" in ctx and "prefer what they have" in ctx
    assert "chicken thighs boneless" in ctx and "eggs large dozen" not in ctx


def test_receipt_upsert_adds_to_an_existing_open_item(db, receipts_on):
    import receipts
    from models import get_session, PantryItem
    user = _user(db)
    receipts.ingest_receipt(user.id, {"store": "safeway", "items": [{"name": "eggs large dozen", "qty": 1, "unit": "dozen", "is_food": True, "est_grams": 600}]})
    receipts.ingest_receipt(user.id, {"store": "safeway", "items": [{"name": "eggs large dozen", "qty": 1, "unit": "dozen", "is_food": True, "est_grams": 600}]})
    s = get_session()
    try:
        rows = s.query(PantryItem).filter_by(user_id=user.id).all()
        assert len(rows) == 1 and rows[0].qty == 2 and rows[0].est_grams == 1200
    finally:
        s.close()

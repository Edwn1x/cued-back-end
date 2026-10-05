"""
Receipt → restaurant MEAL vs grocery PANTRY (live 2026-10-03, user 31). A Chick-fil-A
dinner receipt was stocked as groceries with USDA garbage canonical items ('fries md' →
'calamari, fried', 'mac&chz sm' → 'big mac (mcdonalds)'), the reply said "logged" while
no meal existed, and the later "that was a meal" correction had no un-stock path.

Covered: the exact CFA receipt → ONE grouped meal (4–5 items, plausible calories), 0
pantry rows, a reply that says "logged" + the items; a grocery receipt still stocks, with
the USDA similarity floor accepting a real match and rejecting the garbage; an unsure
receipt asks one question and writes nothing, the answer resolves in code; the pantry
un-stock path (manage_log entity='pantry') + log_meal in one turn; and ranking — a
floor-rejected item (protein None) never outranks a real matched one.
All model / USDA calls are stubbed.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/AAAA"}}

CFA_RECEIPT = {
    "kind": "restaurant",
    "merchant": "chick-fil-a",
    "items": [
        {"name": "nug 12ct", "qty": 1, "unit": "each", "price": 6.95, "is_food": True, "est_grams": 170,
         "calories": 380, "protein_g": 40, "carbs_g": 16, "fat_g": 17},
        {"name": "strips 3ct", "qty": 1, "unit": "each", "price": 5.69, "is_food": True, "est_grams": 130,
         "calories": 310, "protein_g": 29, "carbs_g": 16, "fat_g": 14},
        {"name": "mac&chz sm", "qty": 1, "unit": "each", "price": 3.55, "is_food": True, "est_grams": 150,
         "calories": 260, "protein_g": 12, "carbs_g": 20, "fat_g": 14},
        {"name": "fries md", "qty": 1, "unit": "each", "price": 2.55, "is_food": True, "est_grams": 125,
         "calories": 420, "protein_g": 5, "carbs_g": 45, "fat_g": 24},
        {"name": "cfa sauce", "qty": 4, "unit": "each", "price": 0.0, "is_food": True, "est_grams": 112,
         "calories": 560, "protein_g": 0, "carbs_g": 24, "fat_g": 52},
        {"name": "tax", "qty": 1, "unit": None, "price": 1.72, "is_food": False, "est_grams": 0},
    ],
}

TJ_RECEIPT = {
    "kind": "grocery",
    "merchant": "trader joe's",
    "items": [
        {"name": "eggs large dozen", "qty": 1, "unit": "dozen", "price": 3.99, "is_food": True, "est_grams": 600},
        {"name": "chicken thighs boneless", "qty": 2.1, "unit": "lb", "price": 9.77, "is_food": True, "est_grams": 950},
        {"name": "greek yogurt plain 32oz", "qty": 1, "unit": "each", "price": 4.49, "is_food": True, "est_grams": 907},
    ],
}

# What a real USDA search returns as hits[0..2] for each line (description, protein/100g).
USDA_HITS = {
    "fries md": [("calamari, fried", 15.3), ("fried onion rings", 3.0)],
    "mac&chz sm": [("big mac (mcdonalds)", 12.0), ("macaroni and cheese, boxed", 9.0)],
    "cfa sauce": [("beef and potatoes with cream sauce, baby food", 4.0)],
    "strips 3ct": [("bacon strip, meatless", 14.0)],
    "nug 12ct": [],
    "greek yogurt": [("yogurt, greek, plain, nonfat", 10.2)],
    "greek yogurt plain 32oz": [("yogurt, greek, plain, nonfat", 10.2)],
    "eggs large dozen": [("egg, whole, raw, fresh", 12.6)],
    "chicken thighs boneless": [("chicken, thigh, boneless, skinless, raw", 19.7)],
}


@pytest.fixture
def receipts_on(monkeypatch):
    import config, receipts
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", True)
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "RECEIPT_RESTAURANT_MEAL_ENABLED", True)
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "receipt")
    import usda
    monkeypatch.setattr(usda, "search_usda",
                        lambda q, page_size=5: [{"description": d, "protein_g": p} for d, p in USDA_HITS.get(q, [])])


def _user(db, **kw):
    base = dict(name="Nau", onboarding_step=3, protein_target=139, user_timezone="America/Los_Angeles",
                cooking_situation="cooks")
    base.update(kw)
    return make_user(db, **base)


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).order_by(Meal.id).all()
    finally:
        s.close()


def _pantry(user_id, active_only=False):
    from models import get_session, PantryItem
    s = get_session()
    try:
        q = s.query(PantryItem).filter_by(user_id=user_id)
        if active_only:
            q = q.filter(PantryItem.depleted_at.is_(None))
        return q.order_by(PantryItem.id).all()
    finally:
        s.close()


# ─── restaurant → one grouped meal ──────────────────────────────────────────

def test_cfa_receipt_is_one_grouped_meal_and_zero_pantry_rows(db, receipts_on, monkeypatch):
    import receipts
    from models import get_session, Signal, User
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: CFA_RECEIPT)
    reply = receipts.handle_receipt_image(user.id, IMG)

    meals = _meals(user.id)
    assert 4 <= len(meals) <= 5
    assert len({m.meal_group_id for m in meals}) == 1 and meals[0].meal_group_id   # ONE grouped meal
    by_desc = {m.description: m for m in meals}
    assert by_desc["strips 3ct"].calories == 310 and by_desc["mac&chz sm"].calories == 260
    assert by_desc["fries md"].calories == 420
    assert "cfa sauce x4" in by_desc and by_desc["cfa sauce x4"].calories == 560
    assert all(m.source == "photo" and "chick-fil-a receipt" in (m.notes or "") for m in meals)
    assert _pantry(user.id) == []                                                  # NOTHING stocked
    # reply is built from the written rows: says logged + the items + where
    assert reply.startswith("got the chick-fil-a receipt — logged it as ")
    for label in ("strips 3ct", "fries md", "mac&chz sm", "nug 12ct", "cfa sauce x4"):
        assert label in reply
    total = sum(m.calories for m in meals)
    assert f"(~{total} cal)" in reply and "fix any if off" in reply
    assert "stocked" not in reply
    s = get_session()
    try:
        assert s.get(User, user.id).calories_today == total                       # totals recomputed
        sig = s.query(Signal).filter_by(user_id=user.id, kind="receipt").one()
        assert sig.payload["kind"] == "restaurant" and len(sig.payload["meal_ids"]) == len(meals)
    finally:
        s.close()


def test_restaurant_receipt_never_calls_usda(db, receipts_on, monkeypatch):
    import receipts, usda
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: CFA_RECEIPT)
    monkeypatch.setattr(usda, "search_usda", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no USDA on a restaurant receipt")))
    assert "logged it as" in receipts.handle_receipt_image(user.id, IMG)


def test_loop_returns_the_restaurant_reply_without_a_model_turn(db, receipts_on, monkeypatch, anthropic_stub):
    import receipts
    from agent_loop import run_agent_loop
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: CFA_RECEIPT)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("the coach model must not run on a receipt")))
    user = _user(db)
    reply = run_agent_loop(user, "", "food_photo", image_data=IMG)
    assert reply.startswith("got the chick-fil-a receipt — logged it as") and len(_meals(user.id)) == 5


# ─── how the kind is decided ────────────────────────────────────────────────

def test_kind_precedence_caption_then_merchant_then_model():
    import receipts
    # a known chain settles it even if the extractor said grocery
    assert receipts.receipt_kind(dict(CFA_RECEIPT, kind="grocery")) == "restaurant"
    assert receipts.receipt_kind(dict(TJ_RECEIPT, kind="restaurant")) == "grocery"
    # the extractor's kind decides for an unknown merchant; 'unsure' asks; missing → grocery (legacy shape)
    unknown = dict(CFA_RECEIPT, merchant="joe's place")
    assert receipts.receipt_kind(dict(unknown, kind="restaurant")) == "restaurant"
    assert receipts.receipt_kind(dict(unknown, kind="grocery")) == "grocery"
    assert receipts.receipt_kind(dict(unknown, kind="unsure")) == "ask"
    assert receipts.receipt_kind({"store": "corner shop", "items": []}) == "grocery"
    # an explicit caption outranks everything
    assert receipts.receipt_kind(dict(unknown, kind="unsure"), caption="just ate this") == "restaurant"
    assert receipts.receipt_kind(dict(unknown, kind="unsure"), caption="groceries for the week") == "grocery"
    assert receipts.receipt_kind(dict(unknown, kind="unsure"), caption="here") == "ask"


# ─── grocery → pantry, with the USDA similarity floor ───────────────────────

def test_grocery_receipt_still_stocks_and_accepts_real_usda_matches(db, receipts_on, monkeypatch):
    import receipts
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: TJ_RECEIPT)
    reply = receipts.handle_receipt_image(user.id, IMG)
    rows = _pantry(user.id)
    assert [r.item for r in rows] == ["egg, whole, raw, fresh", "chicken, thigh, boneless, skinless, raw",
                                      "yogurt, greek, plain, nonfat"]
    assert [r.protein_per_100g for r in rows] == [12.6, 19.7, 10.2]
    assert reply.startswith("got your trader joe's receipt. stocked ") and "set through" in reply
    assert _meals(user.id) == []                                                  # a grocery run is not a meal


@pytest.mark.parametrize("name,merchant,expect_item,expect_pp", [
    ("fries md", "trader joe's", "fries md", None),                 # 'calamari, fried' — head mismatch → rejected
    ("mac&chz sm", "trader joe's", "mac chz sm", None),             # 'big mac (mcdonalds)' — foreign brand → rejected
    ("cfa sauce", "trader joe's", "cfa sauce", None),               # one token of two → 0.5 < floor
    ("strips 3ct", "trader joe's", "strips 3ct", None),             # 'bacon strip, meatless' — one-word line, hit leads with bacon
    ("nug 12ct", "trader joe's", "nug 12ct", None),                 # 0 hits
    ("greek yogurt", "trader joe's", "yogurt, greek, plain, nonfat", 10.2),   # accepted
    ("eggs large dozen", "safeway", "egg, whole, raw, fresh", 12.6),           # sizes/units stripped → accepted
])
def test_canonicalize_floor_and_brand_reject(receipts_on, caplog, name, merchant, expect_item, expect_pp):
    import logging, receipts
    caplog.set_level(logging.INFO, logger="cued.receipts")
    item, pp = receipts.canonicalize(name, merchant=merchant)
    assert (item, pp) == (expect_item, expect_pp)
    if expect_pp is None and USDA_HITS[name]:
        rec = [r for r in caplog.records if "RECEIPT_USDA_REJECTED" in r.getMessage()]
        assert rec and f"name={name!r}" in rec[-1].getMessage() and "score=" in rec[-1].getMessage()


def test_score_hit_examples():
    import receipts
    assert receipts.score_hit("greek yogurt plain 32oz", "Yogurt, Greek, plain, nonfat") == 1.0
    assert receipts.score_hit("eggs large dozen", "Egg, whole, raw, fresh") == 1.0
    assert receipts.score_hit("chicken thighs boneless", "Chicken, thigh, boneless, skinless, raw") == 1.0
    assert receipts.score_hit("fries md", "Calamari, fried") == 0.0
    assert receipts.score_hit("mac&chz sm", "Big Mac (McDonalds)") == 0.5
    assert receipts.score_hit("cfa sauce", "Beef and potatoes with cream sauce, baby food") == 0.5
    assert receipts.score_hit("strips 3ct", "Bacon strip, meatless") == 0.0


def test_floor_rejected_item_never_outranks_a_matched_one(db, receipts_on, monkeypatch):
    """A guessed row (protein None) ranks LAST in PANTRY context and the inventory line,
    even with a huge est_grams — garbage protein used to push it to the top."""
    import receipts
    from agent_loop import build_loop_context
    from models import get_session, User
    user = _user(db)
    mixed = {"kind": "grocery", "merchant": "trader joe's", "items": [
        {"name": "fries md", "qty": 1, "unit": "each", "is_food": True, "est_grams": 5000},
        {"name": "greek yogurt", "qty": 1, "unit": "each", "is_food": True, "est_grams": 100},
    ]}
    receipts.ingest_receipt(user.id, mixed)
    rows = {r.label: r for r in _pantry(user.id)}
    assert rows["fries md"].protein_per_100g is None and rows["greek yogurt"].protein_per_100g == 10.2
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    block = ctx[ctx.index("## PANTRY"):]
    assert block.index("greek yogurt") < block.index("fries md")
    assert f"[id {rows['greek yogurt'].id}]" in block                            # ids for manage_log(entity='pantry')
    assert receipts.inventory_line(user.id).startswith("greek yogurt (1), fries md (1)")


# ─── unsure → one question, no writes; the answer resolves in code ──────────

def test_ambiguous_receipt_asks_one_question_and_writes_nothing(db, receipts_on, monkeypatch):
    import receipts
    from models import get_session, Signal
    user = _user(db)
    unsure = dict(CFA_RECEIPT, kind="unsure", merchant="joe's place")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: unsure)
    reply = receipts.handle_receipt_image(user.id, IMG)
    assert reply == "got the joe's place receipt — that a meal u just ate, or groceries?"
    assert _meals(user.id) == [] and _pantry(user.id) == []
    s = get_session()
    try:
        assert s.query(Signal).filter_by(user_id=user.id, kind="receipt_pending").count() == 1
    finally:
        s.close()
    # an unrelated text leaves it pending (normal turn); "meal" resolves it as a meal
    assert receipts.handle_pending_receipt_reply(user.id, "how's the weather") is None
    out = receipts.handle_pending_receipt_reply(user.id, "that was a meal i just ate")
    assert out.startswith("got the joe's place receipt — logged it as") and len(_meals(user.id)) == 5
    assert _pantry(user.id) == []
    assert receipts.handle_pending_receipt_reply(user.id, "meal") is None        # consumed


def test_pending_answer_groceries_stocks_instead(db, receipts_on, monkeypatch):
    import receipts
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt",
                        lambda img, user_id=None: dict(TJ_RECEIPT, kind="unsure", merchant="corner shop"))
    assert receipts.handle_receipt_image(user.id, IMG).endswith("that a meal u just ate, or groceries?")
    out = receipts.handle_pending_receipt_reply(user.id, "groceries")
    assert out.startswith("got your corner shop receipt. stocked ") and len(_pantry(user.id)) == 3
    assert _meals(user.id) == []


def test_pending_answer_is_handled_in_the_pipeline_before_the_model(db, receipts_on, monkeypatch, driver,
                                                                    anthropic_stub, sms_capture):
    import receipts
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt",
                        lambda img, user_id=None: dict(CFA_RECEIPT, kind="unsure", merchant="joe's place"))
    receipts.handle_receipt_image(user.id, IMG)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run for the pending answer")))
    driver.send(user, "meal")
    assert sms_capture[-1][1].startswith("got the joe's place receipt - logged it as")   # GSM-7 dash
    assert len(_meals(user.id)) == 5


# ─── pantry-clear path: the phantom rows from the incident ──────────────────

PHANTOM = [  # the 5 rows the 2026-10-03 receipt actually wrote
    ("calamari, fried", "fries md", 1, 15.3),
    ("big mac (mcdonalds)", "mac&chz sm", 1, 12.0),
    ("beef and potatoes with cream sauce, baby food", "cfa sauce", 4, 4.0),
    ("bacon strip, meatless", "strips 3ct", 1, 14.0),
    ("nug 12ct", "nug 12ct", 1, None),
]


def _seed_phantoms(user_id):
    from datetime import datetime, timezone
    from models import get_session, PantryItem
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        for item, label, qty, pp in PHANTOM:
            s.add(PantryItem(user_id=user_id, item=item, label=label, qty=qty, unit="each", est_grams=150,
                             protein_per_100g=pp, added_at=now, source="receipt"))
        s.commit()
        return [r.id for r in s.query(PantryItem).filter_by(user_id=user_id).order_by(PantryItem.id).all()]
    finally:
        s.close()


def test_manage_log_pantry_delete_by_id_label_and_receipt_scope(db, receipts_on):
    from agent_tools import handle_manage_log
    user = _user(db)
    ids = _seed_phantoms(user.id)
    out = handle_manage_log(user.id, {"action": "delete", "entity": "pantry", "id": ids[0]})
    assert out.startswith("ok: took 1 pantry item off the list: 'fries md'") and "log_meal" in out
    assert len(_pantry(user.id, active_only=True)) == 4
    out = handle_manage_log(user.id, {"action": "delete", "entity": "pantry", "label": "mac and cheese"})
    assert out.startswith("ok: took 1 pantry item off the list: 'mac&chz sm'")
    # honesty: a wrong id / unknown label / edit is an error, never a false ok
    assert handle_manage_log(user.id, {"action": "delete", "entity": "pantry", "id": ids[0]}).startswith("error")
    assert handle_manage_log(user.id, {"action": "delete", "entity": "pantry", "label": "lobster"}).startswith("error")
    assert handle_manage_log(user.id, {"action": "edit", "entity": "pantry", "id": ids[2], "fields": {"qty": 1}}).startswith("error")
    assert handle_manage_log(user.id, {"action": "delete", "entity": "pantry"}).startswith("error")
    # scope='receipt': the rest of the batch goes in one call
    out = handle_manage_log(user.id, {"action": "delete", "entity": "pantry", "id": ids[2], "scope": "receipt"})
    assert out.startswith("ok: took 3 pantry items off the list:")
    assert _pantry(user.id, active_only=True) == []
    assert "pantry [id" not in handle_manage_log(user.id, {"action": "list"})


def test_that_was_a_meal_logs_it_and_clears_the_phantom_rows_in_one_turn(db, receipts_on, anthropic_stub):
    """The correction turn: the coach calls log_meal (the order, its own macros) AND
    manage_log delete entity='pantry' scope='receipt'; both results come back ok; the
    reply says both. Before: 'fixed now' with the phantom stock still listed."""
    from agent_loop import run_agent_loop
    from models import get_session, User
    from tests._fake_anthropic import ToolUse
    user = _user(db)
    ids = _seed_phantoms(user.id)
    seen = []

    def handler(kw):
        system = json.dumps(kw.get("system"))
        assert "## PANTRY" in system and f"[id {ids[0]}]" in system                  # ids reach the model
        results = [b for m in kw["messages"] for b in (m["content"] if isinstance(m["content"], list) else [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        seen[:] = [r["content"] for r in results]
        if not results:
            return ToolUse("log_meal", {"items": [
                {"description": "chick-fil-a strips 3ct", "calories": 310, "protein_g": 29, "carbs_g": 16, "fat_g": 14},
                {"description": "chick-fil-a mac & cheese small", "calories": 260, "protein_g": 12, "carbs_g": 20, "fat_g": 14},
                {"description": "chick-fil-a fries medium", "calories": 420, "protein_g": 5, "carbs_g": 45, "fat_g": 24},
            ]})
        if len(results) == 1:
            return ToolUse("manage_log", {"action": "delete", "entity": "pantry", "id": ids[0], "scope": "receipt"})
        assert all(r.startswith("ok") for r in seen), seen
        return "my bad — that was dinner, not groceries. logged it (~990 cal) and cleared it from the pantry"

    anthropic_stub.reply_with(handler)
    reply = run_agent_loop(user, "that was a meal i just ate, not groceries", "general")
    assert "logged" in reply and "cleared" in reply
    assert len(seen) == 2 and seen[0].startswith("ok: logged 3 items") and seen[1].startswith("ok: took 5 pantry items")
    meals = _meals(user.id)
    assert len(meals) == 3 and len({m.meal_group_id for m in meals}) == 1
    assert _pantry(user.id, active_only=True) == []                                # phantom rows depleted
    assert all(r.depleted_at is not None for r in _pantry(user.id))
    s = get_session()
    try:
        assert s.get(User, user.id).calories_today == 990
    finally:
        s.close()


def test_flag_off_restores_stock_everything(db, receipts_on, monkeypatch):
    import config, receipts
    monkeypatch.setattr(config, "RECEIPT_RESTAURANT_MEAL_ENABLED", False)
    user = _user(db)
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: CFA_RECEIPT)
    reply = receipts.handle_receipt_image(user.id, IMG)
    assert reply.startswith("got your chick-fil-a receipt. stocked ") and _meals(user.id) == []
    # even then, the floor keeps the garbage out: every line stays as printed, protein None
    assert all(r.protein_per_100g is None and r.item == receipts._norm(r.label) for r in _pantry(user.id))

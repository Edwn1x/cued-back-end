"""
Receipts (series §1). A receipt photo is classified first (one cheap call), itemized
(one JSON call), then routed by WHAT KIND of receipt it is:

  grocery    → canonical foods via USDA (similarity-floored), upserted into `pantry`:
                 got your trader joe's receipt. stocked eggs, chicken thighs, and greek yogurt — you're set through thursday.
  restaurant → ONE grouped meal eaten now (the extractor's own per-item macros), NOTHING
               to the pantry:
                 got the chick-fil-a receipt — logged it as dinner: strips 3ct, fries md, mac&chz sm (~1000 cal). fix any if off
  unsure     → one question, no writes; the answer resolves in code (handle_pending_receipt_reply).

Live 2026-10-03 (user 31): a Chick-fil-A dinner receipt was stocked as groceries, each
line canonicalized to USDA garbage ('fries md' → 'calamari, fried', 'mac&chz sm' → 'big
mac (mcdonalds)'), and the reply said "logged" while no meal existed. The reply is now
built from the rows actually written, and says WHERE they went.

'Set through' = today + floor(protein_on_hand_g / daily protein target), capped at
PANTRY_MAX_STOCKED_DAYS; dropped when there's no target yet. The model estimates
(merchant, items, grams, restaurant macros); code does every sum and every date.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from cost_tracking import track as track_usage
from llm_client import make_client
from models import get_session, User, PantryItem, Signal

logger = logging.getLogger("cued.receipts")

_client = None


def _cl():
    global _client
    if _client is None:
        _client = make_client()
    return _client


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _join_text(content) -> str:
    from agent_loop import _join_text
    return _join_text(content)


# ─── 1.2 classify ───────────────────────────────────────────────────────────

CLASSIFY_PROMPT = ("Classify this photo with ONE word: meal (food on a plate/bowl/wrapper, a menu, "
                   "a nutrition label), receipt (a store or restaurant receipt/itemized bill), or other. "
                   "Reply with exactly one of: meal, receipt, other.")


def classify_image(image_data: dict, user_id=None) -> str:
    try:
        r = _cl().messages.create(model=config.RECEIPT_CLASSIFIER_MODEL, max_tokens=5,
                                  messages=[{"role": "user", "content": [image_data, {"type": "text", "text": CLASSIFY_PROMPT}]}])
        track_usage(user_id, "receipts.classify_image", config.RECEIPT_CLASSIFIER_MODEL, r)
        word = (_join_text(r.content) or "").strip().lower().strip(".").split()[0] if _join_text(r.content) else ""
        kind = word if word in ("meal", "receipt", "other") else "other"
    except Exception as e:  # noqa: BLE001 — a classifier failure must not eat the photo
        logger.warning("RECEIPT_CLASSIFY_FAILED user=%s err=%s — treating as meal", user_id, e)
        kind = "meal"
    logger.info("IMAGE_CLASSIFIED user=%s kind=%s", user_id, kind)
    return kind


# ─── 1.3 extract ────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """This is a receipt. Return ONLY JSON:
{"kind": "<grocery|restaurant|unsure>",
 "merchant": "<store or restaurant name exactly as printed, lowercase>",
 "items": [{"name": "<line item as printed, lowercase>", "food": "<plain-english name of the food, e.g. 'small mac & cheese', 'medium fries' — only when the printed name is abbreviated>",
            "qty": <number>, "unit": "<each|lb|oz|dozen|pack|g|kg|null>",
            "price": <number or null>, "is_food": <true|false>,
            "est_grams": <total edible grams for the line, your best estimate>,
            "calories": <restaurant only: kcal for the WHOLE line (qty × size printed); null for grocery>,
            "protein_g": <restaurant only, int or null>, "carbs_g": <restaurant only, int or null>, "fat_g": <restaurant only, int or null>}]}
Rules:
- kind: "grocery" = a supermarket / grocery / convenience store — ingredients and packaged goods to take home
  (trader joe's, safeway, costco, target...). "restaurant" = a restaurant, fast-food, café, or food-court ORDER —
  prepared dishes, combos, sides, sauces, drinks (chick-fil-a, mcdonald's, chipotle, a sit-down bill with
  table/server/tip lines). The merchant name and the item types decide it. "unsure" ONLY if you genuinely can't tell.
- one entry per line item; is_food=false for bags, tax, tip, service fees, paper towels, toiletries, deposits, discounts;
- est_grams is the TOTAL weight of food for that line (e.g. 'dozen eggs' → 600; '2 lb chicken thighs' → 907;
  'greek yogurt 32oz' → 907).
- restaurant: every food line ALSO gets calories/protein_g/carbs_g/fat_g — your best estimate for the WHOLE line
  (qty × the size printed: 'mac&chz sm' is a small mac & cheese, 'fries md' a medium fries, 'cfa sauce' ×4 is four
  sauce packets). Use the chain's published nutrition when you know it.
- Never invent items that aren't on the receipt."""


# Sentinel: the extractor's JSON was cut off at the token cap (a LONG receipt),
# NOT an unreadable image. handle_receipt_image answers these two cases differently
# so we never blame a fine photo for a receipt that was simply too long to itemize.
TRUNCATED = object()


def extract_receipt(image_data: dict, user_id=None):
    """dict on success, TRUNCATED sentinel when the model hit the token cap
    mid-JSON (receipt too long), or None when it's genuinely unreadable."""
    try:
        r = _cl().messages.create(model=config.RECEIPT_EXTRACTOR_MODEL,
                                  max_tokens=config.RECEIPT_EXTRACTOR_MAX_TOKENS,
                                  messages=[{"role": "user", "content": [image_data, {"type": "text", "text": EXTRACT_PROMPT}]}])
        track_usage(user_id, "receipts.extract_receipt", config.RECEIPT_EXTRACTOR_MODEL, r)
        # Gate on stop_reason BEFORE parsing: a max_tokens stop means the item list is
        # incomplete even if the truncated text happens to parse — treat it as too-long.
        if getattr(r, "stop_reason", None) == "max_tokens":
            logger.warning("RECEIPT_EXTRACT_TRUNCATED user=%s cap=%s — receipt too long",
                           user_id, config.RECEIPT_EXTRACTOR_MAX_TOKENS)
            return TRUNCATED
        text = _join_text(r.content).replace("```json", "").replace("```", "").strip()
        if "{" in text and "}" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None
        return data
    except Exception as e:  # noqa: BLE001
        logger.warning("RECEIPT_EXTRACT_FAILED user=%s err=%s", user_id, e)
        return None


# ─── kind: grocery vs restaurant ────────────────────────────────────────────

# Chains whose name on a receipt (or inside a USDA description) settles the question.
# Normalized form: lowercase, no punctuation ("chick-fil-a" → "chick fil a").
_RESTAURANT_BRANDS = (
    "mcdonalds", "burger king", "wendys", "taco bell", "kfc", "kentucky fried", "popeyes",
    "chick fil a", "chickfila", "cfa", "subway", "dominos", "pizza hut", "papa johns", "starbucks",
    "dunkin", "chipotle", "panera", "sonic", "arbys", "jack in the box", "in n out", "five guys",
    "little caesars", "dennys", "applebees", "olive garden", "cracker barrel", "dairy queen",
    "carls jr", "hardees", "whataburger", "white castle", "panda express", "wingstop",
    "raising canes", "culvers", "shake shack", "del taco", "el pollo loco", "jimmy johns",
    "jersey mikes", "chilis", "outback", "ihop", "waffle house", "qdoba", "moes", "zaxbys",
    "bojangles", "checkers", "nathans", "long john silvers", "red lobster", "buffalo wild wings",
    "tgi fridays", "red robin", "sbarro", "cinnabon", "auntie annes", "krispy kreme", "tim hortons",
    "peets", "jamba", "smoothie king", "sweetgreen", "cava", "noodles company",
    "habit burger", "super duper", "ikes", "cheeseboard", "la burrita", "gypsys", "doordash",
    "ubereats", "uber eats", "grubhub",
)
_RESTAURANT_WORDS = re.compile(r"\b(cafe|caf[eé]|grill|kitchen|restaurant|bistro|diner|pizzeria|taqueria|"
                               r"sushi|ramen|bbq|burgers?|tacos?|boba|tea house|eatery)\b")
_GROCERY_BRANDS = (
    "trader joes", "safeway", "whole foods", "costco", "berkeley bowl", "target", "walmart", "sprouts",
    "grocery outlet", "monterey market", "kroger", "ralphs", "vons", "albertsons", "lucky", "foodsco",
    "food 4 less", "smart final", "99 ranch", "h mart", "aldi", "wegmans", "publix", "heb", "meijer",
    "winco", "raleys", "nob hill", "andronicos", "mollie stones", "amazon fresh", "instacart", "cvs",
    "walgreens", "rite aid", "sams club", "market", "grocery", "supermarket", "foods",
)

# Caption / follow-up words that settle it without a model call.
MEAL_WORDS_RE = re.compile(r"\b(meal|ate|eat|eating|dinner|lunch|breakfast|snack|just had|ordered|"
                           r"takeout|take out|restaurant)\b", re.I)
GROCERY_WORDS_RE = re.compile(r"\b(grocer(?:y|ies)|groceries|stock(?:ed|ing)?|pantry|bought|shopping|"
                              r"fridge|stocked up|picked up|haul)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower().replace("'", ""))).strip()


def _brand_in(text: str, brands) -> str | None:
    t = f" {_norm(text)} "
    for b in brands:
        if f" {b} " in t:
            return b
    return None


def receipt_kind(extraction: dict, caption: str | None = None) -> str:
    """'restaurant' | 'grocery' | 'ask'. Precedence: an explicit caption ("just ate
    this" / "groceries") → a known chain in the merchant name → the extractor's own
    `kind` → (missing kind, legacy extraction) grocery; 'unsure' → ask."""
    if caption:
        if MEAL_WORDS_RE.search(caption) and not GROCERY_WORDS_RE.search(caption):
            return "restaurant"
        if GROCERY_WORDS_RE.search(caption) and not MEAL_WORDS_RE.search(caption):
            return "grocery"
    merchant = merchant_of(extraction)
    if _brand_in(merchant, _RESTAURANT_BRANDS) or _RESTAURANT_WORDS.search(_norm(merchant)):
        return "restaurant"
    if _brand_in(merchant, _GROCERY_BRANDS):
        return "grocery"
    kind = str(extraction.get("kind") or "").strip().lower()
    if kind in ("restaurant", "grocery"):
        return kind
    if kind == "unsure":
        return "ask"
    return "grocery"


def merchant_of(extraction: dict) -> str:
    return (str(extraction.get("merchant") or extraction.get("store") or "").strip().lower())


# ─── canonicalize (USDA) with a similarity floor ────────────────────────────

# Receipt shorthand → the word the USDA description would use.
_ABBREV = {
    "sm": "small", "md": "medium", "med": "medium", "lg": "large", "lrg": "large", "xl": "extra large",
    "ct": "count", "pk": "pack", "pkg": "package", "ea": "each", "dz": "dozen",
    "chz": "cheese", "chs": "cheese", "chse": "cheese", "chkn": "chicken", "chk": "chicken", "ckn": "chicken",
    "bf": "beef", "grnd": "ground", "bnls": "boneless", "sknls": "skinless", "brst": "breast", "thgh": "thigh",
    "org": "organic", "orgnc": "organic", "wht": "white", "whl": "whole", "grk": "greek", "yog": "yogurt",
    "ygrt": "yogurt", "nug": "nuggets", "nugs": "nuggets", "sndwch": "sandwich", "sndw": "sandwich",
    "veg": "vegetable", "frz": "frozen", "frzn": "frozen", "unswt": "unsweetened", "swt": "sweet",
    "choc": "chocolate", "strw": "strawberry", "tmto": "tomato", "ptto": "potato", "pnut": "peanut",
    "btr": "butter", "almnd": "almond", "mlk": "milk", "crm": "cream", "slcd": "sliced", "shrd": "shredded",
    "bkd": "baked", "frd": "fried", "grld": "grilled", "bnna": "banana", "avo": "avocado", "cuc": "cucumber",
}
# Sizes, units, counts, and USDA boilerplate: carry no food identity, so they never score.
_STOP = {
    "small", "medium", "large", "extra", "count", "pack", "package", "each", "oz", "ounce", "lb", "lbs",
    "pound", "g", "kg", "dozen", "x", "of", "the", "a", "an", "and", "or", "with", "without", "w", "per",
    "pc", "pcs", "raw", "fresh", "nfs", "ns", "as", "to", "type", "added", "from", "in", "for", "on",
    "regular", "commercial", "prepared", "cooked", "frozen", "fast", "food", "foods", "brand", "item",
}


def _stem(t: str) -> str:
    if len(t) > 3 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and t.endswith("oes"):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _tokens(text: str) -> list[str]:
    """Content tokens of a receipt line or a USDA description: lowercased, '&' → and,
    abbreviations expanded, anything with a digit (sizes, counts, SKU codes) dropped,
    stop/boilerplate words dropped, light stemming."""
    out = []
    for raw in re.split(r"[^a-z0-9]+", (text or "").lower().replace("&", " and ")):
        if not raw or any(ch.isdigit() for ch in raw):
            continue
        for w in _ABBREV.get(raw, raw).split():
            if w in _STOP:
                continue
            out.append(_stem(w))
    return out


def _hit_brand(description: str) -> str | None:
    """A chain named in a USDA description — '(mcdonalds)' or 'burger king, ...'."""
    paren = re.findall(r"\(([^)]+)\)", description or "")
    for p in paren:
        b = _brand_in(p, _RESTAURANT_BRANDS)
        if b:
            return b
    return _brand_in(description, _RESTAURANT_BRANDS)


def score_hit(name: str, description: str) -> float:
    """How well a USDA description matches a receipt line, in [0, 1].

    Token CONTAINMENT: the share of the receipt line's content tokens (sizes/counts/
    codes stripped, abbreviations expanded) that appear in the hit's description — AND
    the hit's head segment (the text before its first comma, which USDA uses for the
    primary food) must share at least one token with the line, else 0. Containment
    rather than Jaccard because USDA names carry modifiers the receipt never prints
    ('egg, whole, raw, fresh' for 'eggs large dozen'); the head-segment check (and, for a
    one-word line, a first-token match) stops a modifier-only overlap ('bacon strip,
    meatless' for 'strips', 'calamari, fried' for 'fries') from passing."""
    r = set(_tokens(name))
    if not r:
        return 0.0
    desc = re.sub(r"\([^)]*\)", " ", description or "")
    h = set(_tokens(desc))
    head_tokens = _tokens(desc.split(",")[0])
    if not (r & set(head_tokens)):
        return 0.0
    if len(r) == 1 and head_tokens and head_tokens[0] not in r:
        # A one-word line names the food itself ('strips', 'fries'): the hit must lead with
        # it, else the overlap is a modifier ('bacon strip, meatless', 'calamari, fried').
        return 0.0
    return len(r & h) / len(r)


def canonicalize(name: str, merchant: str | None = None) -> tuple[str, float | None]:
    """(canonical item, protein per 100 g) via USDA — accepted only when the best hit
    scores ≥ RECEIPT_USDA_MIN_SCORE (see score_hit) and doesn't name a DIFFERENT chain
    than the merchant. Otherwise the printed name (lightly normalized) and None, so a
    guessed row never carries protein it doesn't have."""
    fallback = _norm(name)[:80] or name.lower()[:80]
    try:
        from usda import search_usda
        hits = search_usda(name, page_size=3)
    except Exception as e:  # noqa: BLE001 — UsdaUnavailable or anything else: keep the line
        logger.info("RECEIPT_USDA_MISS name=%r err=%s", name, e)
        return fallback, None
    merchant_n = _norm(merchant or "")
    best, best_score, rejected = None, 0.0, []     # rejected: (description, score, reason)
    for h in hits or []:
        desc = (h.get("description") or "")
        s = score_hit(name, desc)
        brand = _hit_brand(desc)
        if brand and brand not in merchant_n:
            # 'big mac (mcdonalds)' for a Trader Joe's (or Chick-fil-A) line: another chain's food.
            rejected.append((desc, s, f"foreign_brand={brand}"))
            continue
        if s > best_score:
            best, best_score = h, s
    if best is not None and best_score >= config.RECEIPT_USDA_MIN_SCORE:
        return (best.get("description") or fallback).lower()[:80], best.get("protein_g")
    if hits:
        top = max(rejected + ([((best or {}).get("description"), best_score, "below_floor")] if best else []),
                  key=lambda t: t[1], default=(None, 0.0, "no_candidate"))
        logger.info("RECEIPT_USDA_REJECTED name=%r best=%r score=%.2f reason=%s floor=%.2f",
                    name, top[0], top[1], top[2], config.RECEIPT_USDA_MIN_SCORE)
    return fallback, None


def _weekday_name(d) -> str:
    return d.strftime("%A").lower()


def _listed(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _food_items(extraction: dict) -> list[dict]:
    out = []
    for it in extraction.get("items") or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        if it.get("is_food") is False:
            continue
        out.append(it)
    return out


def _num(v, cast=float):
    try:
        return cast(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ─── grocery → pantry ───────────────────────────────────────────────────────

def ingest_receipt(user_id: int, extraction: dict) -> str | None:
    """Upsert pantry rows + one signals row; return the ONE reply line (built from the
    rows actually written), or None when nothing edible was found."""
    store = merchant_of(extraction) or "the store"
    rows = []
    for it in _food_items(extraction):
        canonical, pp100 = canonicalize(str(it["name"]), merchant=store)
        grams = _num(it.get("est_grams"))
        qty = _num(it.get("qty"))
        protein_g = (grams or 0) * (pp100 or 0) / 100.0
        rows.append({"item": canonical, "label": str(it["name"]).strip().lower()[:80], "qty": qty,
                     "unit": (it.get("unit") or None), "est_grams": grams, "pp100": pp100, "protein_g": protein_g})
    if not rows:
        return None

    session = get_session()
    try:
        user = session.get(User, user_id)
        now = _utcnow()
        written = []
        for r in rows:
            existing = (session.query(PantryItem)
                        .filter(PantryItem.user_id == user_id, PantryItem.item == r["item"], PantryItem.depleted_at.is_(None))
                        .first())
            if existing:
                existing.qty = (existing.qty or 0) + (r["qty"] or 0) if r["qty"] is not None else existing.qty
                existing.est_grams = (existing.est_grams or 0) + (r["est_grams"] or 0)
                existing.added_at, existing.source, existing.label = now, "receipt", r["label"]
                if r["pp100"] is not None:
                    existing.protein_per_100g = r["pp100"]
                written.append(existing)
            else:
                row = PantryItem(user_id=user_id, item=r["item"], label=r["label"], qty=r["qty"], unit=r["unit"],
                                 est_grams=r["est_grams"], protein_per_100g=r["pp100"], added_at=now, source="receipt")
                session.add(row)
                written.append(row)
        session.add(Signal(user_id=user_id, kind="receipt", ts=now, source="photo",
                           payload={"kind": "grocery", "store": store,
                                    "items": [{k: v for k, v in r.items() if k != "protein_g"} for r in rows]}))
        session.flush()
        # Reply from the rows as WRITTEN (labels read back off the flushed rows), not the plan.
        written_labels = [(w.label, r["protein_g"]) for w, r in zip(written, rows)]
        session.commit()
        target = user.protein_target
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    finally:
        session.close()

    top = [lbl for lbl, _p in sorted(written_labels, key=lambda t: -t[1])[:3]]
    reply = f"got your {store} receipt. stocked {_listed(top)}"
    protein_on_hand = sum(r["protein_g"] for r in rows)
    if target and protein_on_hand > 0:
        days = min(int(protein_on_hand // target), config.PANTRY_MAX_STOCKED_DAYS)
        through = datetime.now(tz).date() + timedelta(days=days)
        reply += f" — you're set through {_weekday_name(through)}."
    else:
        reply += "."
    logger.info("RECEIPT_INGESTED user=%s kind=grocery store=%r items=%d protein_on_hand=%.0f target=%s",
                user_id, store, len(rows), protein_on_hand, target)
    return reply


# ─── restaurant → one grouped meal ──────────────────────────────────────────

_MACROS = ("calories", "protein_g", "carbs_g", "fat_g")


def ingest_restaurant_receipt(user_id: int, extraction: dict) -> str | None:
    """Write the order as ONE grouped meal eaten now (log_meal's write path: shared
    meal_group_id, totals recomputed) — nothing touches the pantry. The reply is built
    from the meal rows actually written."""
    from agent_tools import log_meal_batch, _meal_slot
    merchant = merchant_of(extraction) or "the restaurant"
    items = []
    for it in _food_items(extraction):
        name = str(it.get("food") or it["name"]).strip().lower()[:80]
        qty = _num(it.get("qty"))
        if qty and qty > 1 and not re.search(rf"\b(x\s*{qty:g}|{qty:g}\s*(ct|x|pc|pcs))\b", name):
            name = f"{name} x{qty:g}"
        macros = {}
        for m in _MACROS:
            v = _num(it.get(m), int)
            if v is None:
                logger.info("RECEIPT_RESTAURANT_MACRO_MISSING user=%s item=%r macro=%s → 0", user_id, name, m)
                v = 0
            macros[m] = max(0, v)
        items.append({"description": name, **macros})
    if not items:
        return None

    now = _utcnow()
    res = log_meal_batch(user_id, items, source="photo", confidence="medium",
                         notes=f"from {merchant} receipt", when=now)
    rows = res["rows"]
    session = get_session()
    try:
        user = session.get(User, user_id)
        tz = ZoneInfo((user.user_timezone if user else None) or "America/Los_Angeles")
        session.add(Signal(user_id=user_id, kind="receipt", ts=now, source="photo",
                           payload={"kind": "restaurant", "store": merchant, "meal_group_id": res["group_id"],
                                    "meal_ids": [r["id"] for r in rows],
                                    "items": [{"name": r["description"], "calories": r["calories"],
                                               "protein_g": r["protein_g"]} for r in rows]}))
        session.commit()
    finally:
        session.close()

    slot = _meal_slot(now, tz)
    names = [r["description"] for r in rows]
    shown = names if len(names) <= 5 else names[:5] + [f"+{len(names) - 5} more"]
    total = sum(r["calories"] or 0 for r in rows)
    reply = f"got the {merchant} receipt — logged it as {slot}: {', '.join(shown)} (~{total} cal). fix any if off"
    logger.info("RECEIPT_INGESTED user=%s kind=restaurant store=%r items=%d cal=%d group=%s",
                user_id, merchant, len(rows), total, res["group_id"])
    return reply


# ─── unsure → ask, resolve in code ──────────────────────────────────────────

PENDING_KIND = "receipt_pending"


def _set_pending(user_id: int, extraction: dict) -> None:
    session = get_session()
    try:
        now = _utcnow()
        for old in session.query(Signal).filter(Signal.user_id == user_id, Signal.kind == PENDING_KIND).all():
            session.delete(old)
        session.add(Signal(user_id=user_id, kind=PENDING_KIND, ts=now, source="photo", payload=extraction,
                           expires_at=now + timedelta(minutes=config.RECEIPT_PENDING_TTL_MIN)))
        session.commit()
    finally:
        session.close()


def _pop_pending(user_id: int) -> dict | None:
    session = get_session()
    try:
        rows = session.query(Signal).filter(Signal.user_id == user_id, Signal.kind == PENDING_KIND).all()
        live = None
        now = _utcnow()
        for r in rows:
            if r.expires_at is None or r.expires_at > now:
                live = dict(r.payload or {})
            session.delete(r)
        session.commit()
        return live
    finally:
        session.close()


def _ask_kind_line(extraction: dict) -> str:
    m = merchant_of(extraction)
    lead = f"got the {m} receipt — " if m else "got the receipt — "
    return lead + "that a meal u just ate, or groceries?"


def handle_pending_receipt_reply(user_id: int, text: str) -> str | None:
    """A pending 'meal or groceries?' answered in code. Returns the reply line, or None
    when there's nothing pending or the text doesn't answer it (a normal turn)."""
    if not config.RECEIPTS_ENABLED or not text or len(text) > 120:
        return None
    session = get_session()
    try:
        has = (session.query(Signal).filter(Signal.user_id == user_id, Signal.kind == PENDING_KIND).count() > 0)
    finally:
        session.close()
    if not has:
        return None
    meal, groc = bool(MEAL_WORDS_RE.search(text)), bool(GROCERY_WORDS_RE.search(text))
    if meal == groc:
        return None
    data = _pop_pending(user_id)
    if not data:
        return None
    kind = "restaurant" if meal else "grocery"
    logger.info("RECEIPT_PENDING_RESOLVED user=%s kind=%s", user_id, kind)
    return _ingest_by_kind(user_id, data, kind)


def _ingest_by_kind(user_id: int, data: dict, kind: str) -> str:
    if kind == "restaurant" and config.RECEIPT_RESTAURANT_MEAL_ENABLED:
        reply = ingest_restaurant_receipt(user_id, data)
    else:
        reply = ingest_receipt(user_id, data)
    return reply or "got the receipt but nothing on it looked like food to me. send me what you actually bought?"


def handle_receipt_image(user_id: int, image_data: dict, caption: str | None = None) -> str | None:
    """The image-turn entry: classify → (receipt) extract → route by kind → reply.
    Returns None for meal/other so the existing path runs untouched."""
    if not config.RECEIPTS_ENABLED:
        return None
    if classify_image(image_data, user_id) != "receipt":
        return None
    data = extract_receipt(image_data, user_id)
    if data is TRUNCATED:
        # The photo was fine — the receipt was just too long to itemize. Ask for the
        # part that matters instead of blaming the image (which loops when they resend).
        return ("that's a long receipt — too many lines for me to pull clean. just tell me the "
                "main protein stuff you got (meat, eggs, dairy, etc.) and i'll log it")
    if not data:
        return "couldn't pull the items off that one. just type the main things you got and i'll save it"
    kind = receipt_kind(data, caption) if config.RECEIPT_RESTAURANT_MEAL_ENABLED else "grocery"
    logger.info("RECEIPT_KIND user=%s kind=%s model_kind=%s merchant=%r", user_id, kind,
                data.get("kind"), merchant_of(data))
    if kind == "ask":
        if not _food_items(data):
            return "got the receipt but nothing on it looked like food to me. send me what you actually bought?"
        _set_pending(user_id, data)
        return _ask_kind_line(data)
    return _ingest_by_kind(user_id, data, kind)


# ─── 1.4 text updates ───────────────────────────────────────────────────────

DEPLETE_RE = re.compile(
    r"^\s*(?:i\s+)?(?:finished|out of|ran out of|used up|no more|ate the last of|ate all the|all out of|"
    r"we're out of|were out of|done with)\s+(?:the\s+|my\s+)?(?P<what>[a-z][a-z '\-]{1,40}?)\s*[.!]*\s*$", re.I)
INVENTORY_RE = re.compile(
    r"^\s*(?:what(?:'s| is| do i have)?\s+(?:in\s+(?:the|my)\s+(?:fridge|pantry|kitchen)|do i have(?: left| at home)?)|"
    r"what do i have|what'?s in (?:the|my) (?:fridge|pantry)|whats in (?:the|my) (?:fridge|pantry)|what food do i have)\s*\??\s*$", re.I)


def _active_items(session, user_id: int) -> list[PantryItem]:
    return (session.query(PantryItem).filter(PantryItem.user_id == user_id, PantryItem.depleted_at.is_(None))
            .order_by(PantryItem.added_at.desc()).all())


def _match_item(items: list[PantryItem], what: str) -> PantryItem | None:
    w = what.strip().lower().rstrip("s")
    best, score = None, 0.0
    for it in items:
        for cand in (it.label or "", it.item or ""):
            c = cand.lower()
            if w and (w in c or c.rstrip("s") in w):
                return it
            r = difflib.SequenceMatcher(None, w, c).ratio()
            if r > score:
                best, score = it, r
    return best if score >= 0.6 else None


def _fmt_qty(it: PantryItem) -> str:
    if it.qty is None:
        return it.label or it.item
    q = f"{it.qty:g}"
    if it.unit in (None, "each", "pack", "dozen"):
        return f"{it.label or it.item} ({q})"
    return f"{it.label or it.item} (~{q} {it.unit})"


def _ranked(items: list[PantryItem]) -> list[PantryItem]:
    """Matched items (a real USDA protein figure) by protein on hand, then every
    unmatched item (protein None — a floor-rejected or unknown line) LAST, newest
    first. A guessed row never outranks a known one."""
    return sorted(items, key=lambda it: (1 if it.protein_per_100g is None else 0,
                                         -((it.est_grams or 0) * (it.protein_per_100g or 0)),
                                         -(it.added_at.timestamp() if it.added_at else 0)))


def _norm_item(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip().lower())[:80]


def stock_items(user_id: int, items: list[dict], *, source: str = "text") -> dict:
    """Food on hand, NOT eaten — from the coach's stock_pantry tool (a package / groceries /
    meal prep in a photo or text). Each item: label (required), est_grams, qty, unit, and the
    coach's estimate for the WHOLE item as calories / protein_g, stored per 100 g so the
    later log can reuse it. Same open label → merged (grams add, estimate refreshed).
    Returns {"written": [labels], "rejected": [labels]}."""
    written: list[str] = []
    rejected: list[str] = []
    now = _utcnow()
    session = get_session()
    try:
        for it in items or []:
            if not isinstance(it, dict):
                continue
            label = str(it.get("label") or it.get("item") or "").strip()
            if not label:
                rejected.append("?")
                continue
            try:
                grams = float(it["est_grams"]) if it.get("est_grams") not in (None, "") else None
                kcal = float(it["calories"]) if it.get("calories") not in (None, "") else None
                prot = float(it["protein_g"]) if it.get("protein_g") not in (None, "") else None
                qty = float(it["qty"]) if it.get("qty") not in (None, "") else None
            except (TypeError, ValueError):
                rejected.append(label)
                continue
            if grams is not None and grams <= 0:
                grams = None
            kp100 = round(kcal / grams * 100, 1) if (kcal is not None and grams) else None
            pp100 = round(prot / grams * 100, 1) if (prot is not None and grams) else None
            key = _norm_item(label)
            existing = (session.query(PantryItem)
                        .filter(PantryItem.user_id == user_id, PantryItem.item == key, PantryItem.depleted_at.is_(None))
                        .first())
            if existing:
                existing.est_grams = (existing.est_grams or 0) + (grams or 0) or existing.est_grams
                if qty is not None:
                    existing.qty = (existing.qty or 0) + qty
                existing.label, existing.added_at, existing.source = label[:80], now, source[:10]
                if kp100 is not None:
                    existing.kcal_per_100g = kp100
                if pp100 is not None:
                    existing.protein_per_100g = pp100
            else:
                session.add(PantryItem(user_id=user_id, item=key, label=label[:80], qty=qty,
                                       unit=(str(it.get("unit") or "")[:20] or None), est_grams=grams,
                                       protein_per_100g=pp100, kcal_per_100g=kp100, added_at=now, source=source[:10]))
            written.append(label)
        session.commit()
    finally:
        session.close()
    if written:
        logger.info("PANTRY_STOCKED_VIA_TOOL user=%s source=%s items=%s", user_id, source, written)
    return {"written": written, "rejected": rejected}


def _if_eaten(it: PantryItem) -> str:
    """' (~750 cal, 80g protein if eaten)' when the row carries an estimate, else ''."""
    g = it.est_grams or 0
    if not g or (it.kcal_per_100g is None and it.protein_per_100g is None):
        return ""
    bits = []
    if it.kcal_per_100g is not None:
        bits.append(f"~{int(round(it.kcal_per_100g * g / 100))} cal")
    if it.protein_per_100g is not None:
        bits.append(f"{int(round(it.protein_per_100g * g / 100))}g protein")
    return f" ({', '.join(bits)} if eaten)"


def handle_pantry_text(user_id: int, text: str) -> str | None:
    """Deterministic pantry updates. Returns the one reply line, or None (not a pantry text)."""
    if not config.RECEIPTS_ENABLED or not text or len(text) > 80:
        return None
    m = DEPLETE_RE.match(text)
    if m:
        session = get_session()
        try:
            it = _match_item(_active_items(session, user_id), m.group("what"))
            if not it:
                return None   # nothing matching in the pantry — a normal turn
            it.depleted_at = _utcnow()
            session.commit()
            label = it.label or it.item
        finally:
            session.close()
        logger.info("PANTRY_DEPLETED user=%s item=%r", user_id, label)
        return f"noted — {label} is off the list."
    if INVENTORY_RE.match(text):
        return inventory_line(user_id)
    return None


def inventory_line(user_id: int) -> str:
    session = get_session()
    try:
        parts = [_fmt_qty(it) for it in _ranked(_active_items(session, user_id))[:4]]
    finally:
        session.close()
    if not parts:
        return "nothing in the pantry yet — send me a receipt or tell me what you've got."
    return ", ".join(parts) + ". low on everything else."


# ─── 1.5 context ────────────────────────────────────────────────────────────

def pantry_context(user_id: int, limit: int = 12) -> str:
    if not config.RECEIPTS_ENABLED:
        return ""
    session = get_session()
    try:
        items = _ranked(_active_items(session, user_id))[:limit]
        lines = [f"[id {it.id}] {_fmt_qty(it)}{_if_eaten(it)}" for it in items]
    finally:
        session.close()
    if not lines:
        return ""
    return ("## PANTRY (what they have at home — prefer what they have when you suggest a meal)\n"
            "This is stock, NOT food eaten. If they say one of these was a meal they ate (or a "
            "restaurant order that got filed here), log_meal it AND manage_log delete it with "
            "entity='pantry' (the id is for you, never say it). An '(… if eaten)' estimate is the "
            "number to log from when they ate the whole thing — scale it if they ate part.\n"
            + "\n".join(f"- {l}" for l in lines))

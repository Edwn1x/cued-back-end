"""
Receipts → pantry (series §1). A receipt photo is classified first (one cheap
call), itemized (one JSON call), mapped to canonical foods via USDA, upserted
into `pantry`, and answered in ONE code-built message:

    got your trader joe's receipt. logged eggs, chicken thighs, and greek yogurt — you're stocked through thursday.

'Stocked through' = today + floor(protein_on_hand_g / daily protein target), capped
at PANTRY_MAX_STOCKED_DAYS; dropped when there's no target yet. The model
estimates (store, items, grams); code does every sum and every date.
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

EXTRACT_PROMPT = """This is a store receipt. Return ONLY JSON:
{"store": "<store name exactly as printed, lowercase>",
 "items": [{"name": "<item as printed, lowercase>", "qty": <number>, "unit": "<each|lb|oz|dozen|pack|g|kg|null>",
            "price": <number or null>, "is_food": <true|false>, "est_grams": <total edible grams for the line, your best estimate>}]}
Rules: one entry per line item; is_food=false for bags, tax, paper towels, toiletries, deposits, discounts;
est_grams is the TOTAL weight of food for that line (e.g. 'dozen eggs' → 600; '2 lb chicken thighs' → 907;
'greek yogurt 32oz' → 907). Never invent items that aren't on the receipt."""


def extract_receipt(image_data: dict, user_id=None) -> dict | None:
    try:
        r = _cl().messages.create(model=config.RECEIPT_EXTRACTOR_MODEL, max_tokens=1500,
                                  messages=[{"role": "user", "content": [image_data, {"type": "text", "text": EXTRACT_PROMPT}]}])
        track_usage(user_id, "receipts.extract_receipt", config.RECEIPT_EXTRACTOR_MODEL, r)
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


def canonicalize(name: str) -> tuple[str, float | None]:
    """(canonical item, protein per 100 g) via USDA; the printed name and None on a miss."""
    try:
        from usda import search_usda
        hits = search_usda(name, page_size=3)
        if hits:
            h = hits[0]
            return (h.get("description") or name).lower()[:80], h.get("protein_g")
    except Exception as e:  # noqa: BLE001 — UsdaUnavailable or anything else: keep the line
        logger.info("RECEIPT_USDA_MISS name=%r err=%s", name, e)
    return name.lower()[:80], None


def _weekday_name(d) -> str:
    return d.strftime("%A").lower()


def ingest_receipt(user_id: int, extraction: dict) -> str | None:
    """Upsert pantry rows + one signals row; return the ONE reply line, or None when
    nothing edible was found."""
    store = (extraction.get("store") or "").strip().lower() or "the store"
    rows = []
    for it in extraction.get("items") or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        if it.get("is_food") is False:
            continue
        canonical, pp100 = canonicalize(str(it["name"]))
        try:
            grams = float(it.get("est_grams")) if it.get("est_grams") is not None else None
        except (TypeError, ValueError):
            grams = None
        try:
            qty = float(it.get("qty")) if it.get("qty") is not None else None
        except (TypeError, ValueError):
            qty = None
        protein_g = (grams or 0) * (pp100 or 0) / 100.0
        rows.append({"item": canonical, "label": str(it["name"]).strip().lower()[:80], "qty": qty,
                     "unit": (it.get("unit") or None), "est_grams": grams, "pp100": pp100, "protein_g": protein_g})
    if not rows:
        return None

    session = get_session()
    try:
        user = session.get(User, user_id)
        now = _utcnow()
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
            else:
                session.add(PantryItem(user_id=user_id, item=r["item"], label=r["label"], qty=r["qty"], unit=r["unit"],
                                       est_grams=r["est_grams"], protein_per_100g=r["pp100"], added_at=now, source="receipt"))
        session.add(Signal(user_id=user_id, kind="receipt", ts=now, source="photo",
                           payload={"store": store, "items": [{k: v for k, v in r.items() if k != "protein_g"} for r in rows]}))
        session.commit()
        target = user.protein_target
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    finally:
        session.close()

    top = [r["label"] for r in sorted(rows, key=lambda r: -r["protein_g"])[:3]]
    listed = top[0] if len(top) == 1 else (f"{top[0]} and {top[1]}" if len(top) == 2 else f"{top[0]}, {top[1]}, and {top[2]}")
    reply = f"got your {store} receipt. logged {listed}"
    protein_on_hand = sum(r["protein_g"] for r in rows)
    if target and protein_on_hand > 0:
        days = min(int(protein_on_hand // target), config.PANTRY_MAX_STOCKED_DAYS)
        through = datetime.now(tz).date() + timedelta(days=days)
        reply += f" — you're stocked through {_weekday_name(through)}."
    else:
        reply += "."
    logger.info("RECEIPT_INGESTED user=%s store=%r items=%d protein_on_hand=%.0f target=%s",
                user_id, store, len(rows), protein_on_hand, target)
    return reply


def handle_receipt_image(user_id: int, image_data: dict) -> str | None:
    """The image-turn entry: classify → (receipt) extract → ingest → reply. Returns
    None for meal/other so the existing path runs untouched."""
    if not config.RECEIPTS_ENABLED:
        return None
    if classify_image(image_data, user_id) != "receipt":
        return None
    data = extract_receipt(image_data, user_id)
    if not data:
        return "that looks like a receipt but i couldn't read the lines — try a flatter, brighter shot?"
    reply = ingest_receipt(user_id, data)
    return reply or "got the receipt but nothing on it looked like food to me. send me what you actually bought?"


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
        items = _active_items(session, user_id)
        ranked = sorted(items, key=lambda it: -((it.est_grams or 0) * (it.protein_per_100g or 0)))
        parts = [_fmt_qty(it) for it in ranked[:4]]
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
        items = _active_items(session, user_id)[:limit]
        lines = [_fmt_qty(it) for it in items]
    finally:
        session.close()
    if not lines:
        return ""
    return ("## PANTRY (what they have at home — prefer what they have when you suggest a meal)\n"
            + "\n".join(f"- {l}" for l in lines))

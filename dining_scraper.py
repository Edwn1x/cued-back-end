import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from models import get_session, DiningMenuItem

logger = logging.getLogger(__name__)

PACIFIC = ZoneInfo("America/Los_Angeles")
EXPORT_BASE = "https://dining.berkeley.edu/wp-content/uploads/menus-exportimport"

# Dining commons only. Convenience stores (Den, markets) publish placeholder
# "message" recipes with zero nutrition, so we never fetch them.
#
# The filename token is the <menu location="..."> value with spaces -> underscores.
# Crossroads + Foothill are open year-round; Clark Kerr + Cafe 3 are closed in
# summer and their files 404 — which scrape_all_halls() skips silently. When they
# reopen, if a token is wrong we'll see it in the logs and correct it here.
DINING_HALL_TOKENS = ["Crossroads", "Foothill", "Clark_Kerr", "Cafe_3"]

# Maps the raw <menu location> attribute to our canonical DiningMenuItem.hall key.
def _canonical_hall(location: str) -> str:
    key = (location or "").strip().lower().replace("é", "e")
    key = re.sub(r"\s+", "_", key)
    return {"cafe_3": "cafe3"}.get(key, key)


# The DiningMenuItem columns we extract, matched against the <nutrients> legend by
# name (not by fixed index) so a column reorder upstream can't shift values silently.
# Each entry: (column, predicate over the lowercased legend label).
_NUTRIENT_MATCHERS = {
    "calories":  lambda s: s.startswith("calories"),
    "fat_g":     lambda s: s.startswith("total lipid"),
    "carbs_g":   lambda s: s.startswith("carbohydrate"),
    "fiber_g":   lambda s: s.startswith("total dietary fiber"),
    "protein_g": lambda s: s.startswith("protein"),
}


def _legend_index_map(menu_el: ET.Element) -> dict:
    """Parse the <nutrients> legend on a <menu> into {column: position}."""
    legend_el = menu_el.find("nutrients")
    if legend_el is None or not (legend_el.text or "").strip():
        return {}
    labels = [p.strip() for p in legend_el.text.split("|")]
    index_map = {}
    for i, label in enumerate(labels):
        low = label.lower()
        for col, matches in _NUTRIENT_MATCHERS.items():
            if col not in index_map and matches(low):
                index_map[col] = i
    return index_map


def _num(values: list, idx, cast):
    if idx is None or idx >= len(values):
        return None
    raw = values[idx].strip()
    if raw == "":
        return None
    try:
        return cast(float(raw))
    except (ValueError, TypeError):
        return None


def _normalize_meal_period(mealperiodname: str) -> str:
    # "Summer – Breakfast" -> "breakfast"; "Summer – All Day" -> "all_day"
    part = re.split(r"[-–—]", mealperiodname or "")[-1].strip().lower()
    return re.sub(r"\s+", "_", part) or "unknown"


def _yes_ids(parent: ET.Element, child_tag: str) -> str:
    """Collect the id attrs of child elements whose text is 'Yes', comma-joined."""
    if parent is None:
        return ""
    out = []
    for el in parent.findall(child_tag):
        if (el.text or "").strip().lower() == "yes":
            out.append((el.get("id") or "").strip())
    return ", ".join(t for t in out if t)


def _normalize_dietary(raw_ids: str) -> str:
    # "Vegan Option, Vegetarian Option, Halal" -> "vegan, vegetarian, halal"
    tags = []
    for t in [x.strip().lower() for x in raw_ids.split(",") if x.strip()]:
        tags.append(t.replace(" option", "").strip())
    return ", ".join(tags)


def _is_placeholder(item_name: str, description: str) -> bool:
    blob = f"{item_name} {description}".lower()
    return ("message" in blob) or ("placeholder" in blob) or not item_name


def parse_menu_xml(xml_text: str) -> list[dict]:
    """Parse one hall's daily export into a list of DiningMenuItem-shaped dicts."""
    items = []
    root = ET.fromstring(xml_text)
    # A file may contain several <menu> blocks (one per meal period).
    for menu_el in root.iter("menu"):
        hall = _canonical_hall(menu_el.get("location"))
        meal_period = _normalize_meal_period(menu_el.get("mealperiodname"))
        servedate = (menu_el.get("servedate") or "").strip()  # YYYYMMDD
        scraped_date = (
            f"{servedate[0:4]}-{servedate[4:6]}-{servedate[6:8]}"
            if len(servedate) == 8 else None
        )
        idx = _legend_index_map(menu_el)

        for recipe in menu_el.iter("recipe"):
            item_name = (recipe.get("shortName") or recipe.get("description") or "").strip()
            description = (recipe.get("description") or "").strip()
            if _is_placeholder(item_name, description):
                continue

            vals = (recipe.get("nutrients") or "").split("|")
            serving_unit = (recipe.get("servingSizeUnit") or "").strip()
            serving_size = " ".join(
                p for p in [(recipe.get("servingSize") or "").strip(), serving_unit] if p
            ) or None

            items.append({
                "scraped_date": scraped_date,
                "hall": hall,
                "meal_period": meal_period,
                "station": (recipe.get("category") or "").strip() or None,
                "item_name": item_name,
                "calories":  _num(vals, idx.get("calories"), int),
                "protein_g": _num(vals, idx.get("protein_g"), lambda x: round(x, 1)),
                "carbs_g":   _num(vals, idx.get("carbs_g"), lambda x: round(x, 1)),
                "fat_g":     _num(vals, idx.get("fat_g"), lambda x: round(x, 1)),
                "fiber_g":   _num(vals, idx.get("fiber_g"), lambda x: round(x, 1)),
                "serving_size": serving_size,
                "allergens": _yes_ids(recipe.find("allergens"), "allergen"),
                "dietary_tags": _normalize_dietary(_yes_ids(recipe.find("dietaryChoices"), "dietaryChoice")),
            })
    return items


def scrape_hall(token: str, date_str: str) -> list[dict]:
    """Fetch + parse one hall's export for date_str (YYYYMMDD). [] on 404/closed."""
    url = f"{EXPORT_BASE}/{token}_{date_str}.xml"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 404:
            logger.info(f"[dining_scraper] {token}: no export for {date_str} (closed) — skipping")
            return []
        resp.raise_for_status()
        items = parse_menu_xml(resp.text)
        logger.info(f"[dining_scraper] {token}: parsed {len(items)} items for {date_str}")
        return items
    except ET.ParseError as e:
        logger.error(f"[dining_scraper] {token}: XML parse failed: {e}")
        return []
    except Exception as e:
        logger.warning(f"[dining_scraper] {token}: fetch failed ({url}): {e}")
        return []


def scrape_all_halls() -> int:
    """Scrape every open dining commons for today (Pacific) and persist."""
    today = datetime.now(PACIFIC)
    date_str = today.strftime("%Y%m%d")
    scraped_date = today.strftime("%Y-%m-%d")

    all_items = []
    for token in DINING_HALL_TOKENS:
        items = scrape_hall(token, date_str)
        # Trust the file's own servedate; fall back to today if missing.
        for item in items:
            item.setdefault("scraped_date", scraped_date)
            if not item.get("scraped_date"):
                item["scraped_date"] = scraped_date
        all_items.extend(items)

    if all_items:
        save_menu_items(all_items, scraped_date)
    clear_old_menus()
    logger.info(f"[dining_scraper] Scraped {len(all_items)} items total for {scraped_date}")
    return len(all_items)


def save_menu_items(items: list[dict], scraped_date: str):
    if not items:
        return
    halls_in_batch = {item["hall"] for item in items}
    session = get_session()
    try:
        # Idempotent: clear this date+hall set, then re-insert. Survives re-runs.
        session.query(DiningMenuItem).filter(
            DiningMenuItem.scraped_date == scraped_date,
            DiningMenuItem.hall.in_(halls_in_batch),
        ).delete(synchronize_session=False)

        for item in items:
            session.add(DiningMenuItem(
                scraped_date=item.get("scraped_date", scraped_date),
                hall=item["hall"],
                meal_period=item.get("meal_period", ""),
                station=item.get("station"),
                item_name=item.get("item_name", ""),
                calories=item.get("calories"),
                protein_g=item.get("protein_g"),
                carbs_g=item.get("carbs_g"),
                fat_g=item.get("fat_g"),
                fiber_g=item.get("fiber_g"),
                serving_size=item.get("serving_size"),
                allergens=item.get("allergens"),
                dietary_tags=item.get("dietary_tags"),
            ))
        session.commit()
        logger.info(f"[dining_scraper] Saved {len(items)} items for {scraped_date}")
    except Exception as e:
        session.rollback()
        logger.error(f"[dining_scraper] Failed to save menu items: {e}")
    finally:
        session.close()


def clear_old_menus():
    cutoff = (datetime.now(PACIFIC) - timedelta(days=7)).strftime("%Y-%m-%d")
    session = get_session()
    try:
        deleted = (
            session.query(DiningMenuItem)
            .filter(DiningMenuItem.scraped_date < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info(f"[dining_scraper] Cleared {deleted} stale rows older than {cutoff}")
    except Exception as e:
        session.rollback()
        logger.error(f"[dining_scraper] Failed to clear old menus: {e}")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Phase 4 — query + formatting helpers the nutrition agent calls at request time
# ---------------------------------------------------------------------------

HALL_DISPLAY = {
    "crossroads": "Crossroads",
    "foothill": "Foothill",
    "clark_kerr": "Clark Kerr",
    "cafe3": "Cafe 3",
}

HALL_ALIASES = {
    "crossroads": ["crossroads", "xroads", "cross roads", "croads"],
    "foothill": ["foothill", "foot hill"],
    "clark_kerr": ["clark kerr", "clark-kerr", "ckc", "clark"],
    "cafe3": ["cafe 3", "café 3", "cafe3", "café3", "c3"],
}

# A user message that should trigger menu injection even without a named hall.
GENERIC_DINING_TERMS = [
    "dining hall", "dining commons", "dining hal", "meal swipe", "meal plan",
    "on campus", "the dc", "what's on the menu", "whats on the menu",
]

# Maps each canonical Berkeley allergen id (lowercased, as stored on
# DiningMenuItem.allergens) to the whole-word triggers we look for in the user's
# free-text restrictions ("allergies, dislikes"). Word-boundary matched, so "nut"
# won't fire on "nutrition" and "egg" won't fire on "eggplant" — while still
# catching natural phrasing like "peanut allergy", "dairy", or "no shellfish".
# Output keys MUST stay in Berkeley's allergen vocabulary so get_todays_menu's
# set-intersection against stored allergen ids still matches.
_ALLERGEN_SYNONYMS = {
    "milk":      ["milk", "dairy", "lactose", "lactaid", "casein", "whey"],
    "egg":       ["egg", "eggs"],
    "fish":      ["fish", "salmon", "tuna", "tilapia", "cod", "halibut"],
    "shellfish": ["shellfish", "shrimp", "crab", "lobster", "prawn", "clam",
                  "oyster", "mussel", "scallop", "crawfish"],
    "tree nuts": ["tree nut", "tree nuts", "almond", "walnut", "cashew", "pecan",
                  "pistachio", "hazelnut", "macadamia"],
    "wheat":     ["wheat"],
    "peanuts":   ["peanut", "peanuts", "groundnut"],
    "soybeans":  ["soy", "soya", "soybean", "soybeans", "edamame", "tofu"],
    "gluten":    ["gluten", "celiac", "coeliac"],
    "sesame":    ["sesame", "tahini"],
    "pork":      ["pork", "bacon", "ham", "pig"],
}
# A bare "nut"/"nuts" with no qualifier (e.g. "no nuts", "nut allergy") implies
# both tree nuts and peanuts.
_GENERIC_NUT_ALLERGENS = ["tree nuts", "peanuts"]


def _excluded_allergens(restrictions: str) -> list[str]:
    """Map a user's free-text restrictions to canonical Berkeley allergen ids."""
    low = (restrictions or "").lower()
    if not low.strip():
        return []
    out = set()
    for canonical, triggers in _ALLERGEN_SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(t)}\b", low) for t in triggers):
            out.add(canonical)
    # A bare "nut"/"nuts" implies both nut families — but only when it isn't already
    # qualified (so "tree nuts" alone doesn't also exclude peanuts, and vice versa).
    unqualified = re.sub(r"\b(?:tree|pea|ground)\s*nuts?\b", " ", low)
    if re.search(r"\bnuts?\b", unqualified):
        out.update(_GENERIC_NUT_ALLERGENS)
    return sorted(out)


def detect_halls(message: str) -> list[str]:
    """Return canonical hall keys named in the message (empty if none)."""
    low = (message or "").lower()
    hits = []
    for hall, aliases in HALL_ALIASES.items():
        if any(a in low for a in aliases):
            hits.append(hall)
    return hits


def mentions_dining(message: str) -> bool:
    low = (message or "").lower()
    return bool(detect_halls(message)) or any(t in low for t in GENERIC_DINING_TERMS)


def _today_pacific() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def ensure_today_scraped() -> None:
    """
    Self-healing freshness: if we have no menu rows for today (Pacific) yet,
    scrape all halls once, right now. This keeps dining answers correct even
    when the daily background cron never fires (e.g. the web container sleeps
    when idle, so the in-memory scheduler's 5:30 AM job never runs). At most one
    scrape_all_halls() per day — once today's rows exist, this is a no-op.
    """
    today = _today_pacific()
    session = get_session()
    try:
        have_today = session.query(DiningMenuItem.id).filter(
            DiningMenuItem.scraped_date == today
        ).first() is not None
    finally:
        session.close()
    if have_today:
        return
    logger.info(f"[dining_scraper] no rows for {today} yet — lazy scraping on demand")
    try:
        scrape_all_halls()
    except Exception as e:
        logger.warning(f"[dining_scraper] lazy scrape failed: {e}")


def get_todays_menu(halls: list[str] = None, meal_period: str = None,
                    exclude_allergens: list[str] = None) -> list[DiningMenuItem]:
    """Query today's (Pacific) menu rows, optionally filtered by hall/meal/allergen."""
    session = get_session()
    try:
        q = session.query(DiningMenuItem).filter(
            DiningMenuItem.scraped_date == _today_pacific()
        )
        if halls:
            q = q.filter(DiningMenuItem.hall.in_(halls))
        if meal_period:
            # Named meal is a narrowing hint, not a hard gate: always also include
            # all_day-labeled rows so a hall using "All Day" never drops out.
            q = q.filter(DiningMenuItem.meal_period.in_([meal_period, "all_day"]))
        rows = q.order_by(
            DiningMenuItem.hall, DiningMenuItem.meal_period,
            DiningMenuItem.station, DiningMenuItem.item_name,
        ).all()

        if exclude_allergens:
            ex = {a.strip().lower() for a in exclude_allergens}
            rows = [
                r for r in rows
                if not (ex & {a.strip().lower() for a in (r.allergens or "").split(",") if a.strip()})
            ]
        # Detach so callers can use rows after the session closes.
        session.expunge_all()
        return rows
    finally:
        session.close()


def format_menu_for_coach(items: list[DiningMenuItem]) -> str:
    """Compact, macro-forward rendering grouped by hall -> meal -> station."""
    if not items:
        return "No dining hall menu data available for today (halls may be closed or not yet published)."

    lines = []
    cur_hall = cur_meal = cur_station = None
    for it in items:
        if it.hall != cur_hall:
            cur_hall, cur_meal, cur_station = it.hall, None, None
            lines.append(f"\n### {HALL_DISPLAY.get(it.hall, it.hall)}")
        if it.meal_period != cur_meal:
            cur_meal, cur_station = it.meal_period, None
            lines.append(f"**{it.meal_period.replace('_', ' ').title()}**")
        if it.station != cur_station:
            cur_station = it.station
            if cur_station:
                lines.append(f"_{cur_station}_")

        macros = []
        if it.calories is not None:
            macros.append(f"{it.calories} cal")
        if it.protein_g is not None:
            macros.append(f"{it.protein_g}g protein")
        if it.carbs_g is not None:
            macros.append(f"{it.carbs_g}g carb")
        if it.fat_g is not None:
            macros.append(f"{it.fat_g}g fat")
        macro_str = ", ".join(macros) if macros else "no macros listed"
        serving = f" ({it.serving_size})" if it.serving_size else ""
        tags = f" [{it.dietary_tags}]" if it.dietary_tags else ""
        lines.append(f"- {it.item_name}{serving} — {macro_str}{tags}")
    return "\n".join(lines).strip()


def build_dining_block(user, user_message: str) -> str:
    """
    Returns a '## TODAY'S DINING HALL MENU' block when the message is about
    campus dining, else ''. Filters to named halls (or all open halls), the
    relevant meal period when one is named, and the user's allergen restrictions.
    """
    if not mentions_dining(user_message):
        return ""

    # Make sure today's menu is actually in the DB before we query it — don't
    # depend on the background cron having run.
    ensure_today_scraped()

    halls = detect_halls(user_message) or None  # None -> all open halls today

    low = (user_message or "").lower()
    meal_period = None
    for mp in ("breakfast", "brunch", "lunch", "dinner"):
        if mp in low:
            meal_period = mp
            break

    exclude = _excluded_allergens(getattr(user, "restrictions", ""))

    items = get_todays_menu(halls=halls, meal_period=meal_period,
                            exclude_allergens=exclude or None)
    # Fallback: if a named meal matched nothing (e.g. label mismatch), use the
    # full day's menu so the block is never silently empty when data exists.
    if not items and meal_period:
        items = get_todays_menu(halls=halls, meal_period=None,
                                exclude_allergens=exclude or None)
    if not items:
        return ""
    return f"\n\n## TODAY'S DINING HALL MENU (real Berkeley data — use exact numbers)\n{format_menu_for_coach(items)}"
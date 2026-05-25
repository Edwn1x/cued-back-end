import logging
from datetime import datetime, timezone, timedelta

import requests

from models import get_session, DiningMenuItem

logger = logging.getLogger(__name__)

DINING_HALLS = {
    "crossroads": "https://dining.berkeley.edu/menus/crossroads/",
    "foothill":   "https://dining.berkeley.edu/menus/foothill/",
    "clark_kerr": "https://dining.berkeley.edu/menus/clark-kerr/",
    "cafe3":      "https://dining.berkeley.edu/menus/cafe-3/",
}


def scrape_hall(hall_name: str, url: str) -> list[dict]:
    # TODO: inspect dining.berkeley.edu DOM and implement parser. Stub returns [] until verified.
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        html = resp.text
        logger.info(
            f"[dining_scraper] {hall_name}: fetched {len(html)} bytes. "
            f"First 500 chars of body: {html[:500]!r}"
        )
    except Exception as e:
        logger.warning(f"[dining_scraper] Failed to fetch {hall_name} ({url}): {e}")
    return []


def scrape_all_halls() -> int:
    scraped_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0
    all_items: list[dict] = []
    for hall_name, url in DINING_HALLS.items():
        items = scrape_hall(hall_name, url)
        for item in items:
            item["hall"] = hall_name
            item["scraped_date"] = scraped_date
        all_items.extend(items)
        total += len(items)
    if all_items:
        save_menu_items(all_items, scraped_date)
    clear_old_menus()
    logger.info(f"[dining_scraper] Scraped {total} items for {scraped_date}")
    return total


def save_menu_items(items: list[dict], scraped_date: str):
    if not items:
        return
    halls_in_batch = {item["hall"] for item in items}
    session = get_session()
    try:
        session.query(DiningMenuItem).filter(
            DiningMenuItem.scraped_date == scraped_date,
            DiningMenuItem.hall.in_(halls_in_batch),
        ).delete(synchronize_session=False)

        for item in items:
            row = DiningMenuItem(
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
            )
            session.add(row)
        session.commit()
        logger.info(f"[dining_scraper] Saved {len(items)} items for {scraped_date}")
    except Exception as e:
        session.rollback()
        logger.error(f"[dining_scraper] Failed to save menu items: {e}")
    finally:
        session.close()


def clear_old_menus():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    session = get_session()
    try:
        deleted = (
            session.query(DiningMenuItem)
            .filter(DiningMenuItem.scraped_date < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info(f"[dining_scraper] Cleared {deleted} stale menu rows older than {cutoff}")
    except Exception as e:
        session.rollback()
        logger.error(f"[dining_scraper] Failed to clear old menus: {e}")
    finally:
        session.close()

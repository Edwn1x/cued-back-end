"""
USDA FoodData Central lookup — macro-accuracy Phase D (first external rung).
============================================================================
Generic-food reference macros, per 100 g, for "identifiable but generic" meals no
internal source covers. Facts live-verified 2026-08-01 (INVESTIGATION §4): search
endpoint + api_key param, per-100g values for Foundation/SR Legacy/FNDDS, macro
nutrient ids 1003/1004/1005/1008(KCAL), 429 on rate limit, ~1.0 s measured latency
(hence the hard timeout and the climb-only-when-needed routing).

Failure envelope: EVERY failure (no key, timeout, 429, HTTP error, bad payload)
surfaces as UsdaUnavailable / an empty result the tool handler turns into a clean
"estimate normally" answer — a meal always logs; the lookup only ever adds info.
"""

from __future__ import annotations

import logging
import time

import requests

import config

logger = logging.getLogger("cued.usda")

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
# Branded is deliberately excluded: the branded/restaurant long tail is the web rung.
DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
MAX_RESULTS = 3

# nutrientId -> our field. Energy 1008 only (KCAL); kJ variants ride other ids.
_NUTRIENT_IDS = {1008: "calories", 1003: "protein_g", 1005: "carbs_g", 1004: "fat_g"}


class UsdaUnavailable(Exception):
    """Lookup could not run (timeout / rate limit / HTTP / payload). Carry the
    reason for the log line; the caller degrades, never blocks."""


def _parse_food(food: dict) -> dict:
    out = {"description": food.get("description") or "",
           "data_type": food.get("dataType") or "",
           "calories": None, "protein_g": None, "carbs_g": None, "fat_g": None}
    for n in food.get("foodNutrients") or []:
        field = _NUTRIENT_IDS.get(n.get("nutrientId"))
        if field is None or n.get("value") is None:
            continue
        if field == "calories":
            if (n.get("unitName") or "").upper() != "KCAL":
                continue
            out["calories"] = int(round(n["value"]))
        else:
            out[field] = round(float(n["value"]), 1)
    return out


def search_usda(query: str, page_size: int = 5) -> list[dict]:
    """Top USDA entries for a generic-food query, macros per 100 g. Trusts the
    API's own relevance score ordering. Raises UsdaUnavailable on any failure."""
    if not config.USDA_API_KEY:
        raise UsdaUnavailable("no USDA_API_KEY configured")

    t0 = time.time()
    try:
        # POST with a JSON body: the GET form 400s at the gateway when
        # "Survey (FNDDS)" rides the URL (parens trip the WAF — live finding),
        # and dataType is a real array here instead of an encoding gamble.
        resp = requests.post(SEARCH_URL, params={"api_key": config.USDA_API_KEY},
                             json={"query": query, "dataType": DATA_TYPES,
                                   "pageSize": page_size},
                             timeout=config.USDA_TIMEOUT_S)
        if resp.status_code == 429:
            raise UsdaUnavailable("rate limited (429)")
        resp.raise_for_status()
        payload = resp.json()
    except UsdaUnavailable:
        raise
    except Exception as e:
        logger.warning("USDA_LOOKUP_FAILED q=%r err=%s", query, e)
        raise UsdaUnavailable(str(e)) from e

    foods = payload.get("foods") or []
    results = [_parse_food(f) for f in foods[:MAX_RESULTS]]
    ms = int((time.time() - t0) * 1000)
    logger.info("USDA_LOOKUP q=%r ms=%d hits=%s returned=%d",
                query, ms, payload.get("totalHits"), len(results))
    return results

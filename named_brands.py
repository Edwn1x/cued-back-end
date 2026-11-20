"""
Named brands, chains and venues in a meal description — the ones with PUBLISHED
nutrition the coach should look up instead of guessing.

Live 2026-11-20 12:13 PT (user 51): "Gonna eat an insomnia cc cookie" was logged as
"insomnia classic chocolate chunk cookie — estimate" at 370 cal / 18g fat; the posted
number is 250 / 12. The model recognized the brand and the exact item and still
guessed, because the routing ladder put the web rung below "generic food". Over the
prior 60 days 99 logged rows named a brand or venue; 3 carried a source. This module is
the code side of the fix: `names_brand()` says WHICH brand a description names (or
None), so handle_log_meal can put the affordance in the tool result at the moment of
the write (the surface that held when description-level rules did not — the
macro-accuracy lesson).

Dining halls with a scraped menu (dining_scraper.HALL_ALIASES) are NOT here: they have
their own tool (match_dining_item) and their own refine path in handle_log_meal.
Add a brand = one entry below, or NAMED_BRANDS_EXTRA=comma,separated in the env.
"""
from __future__ import annotations

import os
import re

# Display name → the spellings people text. Matched on word boundaries, case-insensitive;
# an entry with a space matches that phrase. Keep short/ambiguous words out unless they
# are only ever the brand ("subway" the sandwich is fine; "prime" alone is not).
BRANDS: dict[str, tuple[str, ...]] = {
    # chains — fast food / fast casual
    "chipotle": ("chipotle",),
    "starbucks": ("starbucks", "sbux"),
    "insomnia cookies": ("insomnia cookies", "insomnia cookie", "insomnia"),
    "crumbl": ("crumbl",),
    "in-n-out": ("in-n-out", "in n out", "in and out burger", "innout"),
    "mcdonald's": ("mcdonald's", "mcdonalds", "mcd", "mickey d's"),
    "taco bell": ("taco bell",),
    "chick-fil-a": ("chick-fil-a", "chick fil a", "chickfila", "cfa"),
    "panda express": ("panda express",),
    "sweetgreen": ("sweetgreen",),
    "subway": ("subway",),
    "dunkin": ("dunkin", "dunkin'", "dunkin donuts"),
    "wingstop": ("wingstop",),
    "popeyes": ("popeyes",),
    "shake shack": ("shake shack",),
    "panera": ("panera",),
    "jamba": ("jamba", "jamba juice"),
    "cava": ("cava",),
    "five guys": ("five guys",),
    "wendy's": ("wendy's", "wendys"),
    "burger king": ("burger king",),
    "domino's": ("domino's", "dominos"),
    "pizza hut": ("pizza hut",),
    "little caesars": ("little caesars", "little caesar's"),
    "papa john's": ("papa john's", "papa johns"),
    "ihop": ("ihop",),
    "denny's": ("denny's", "dennys"),
    "kfc": ("kfc",),
    "raising cane's": ("raising cane's", "raising canes", "cane's", "canes"),
    "el pollo loco": ("el pollo loco",),
    "chick'n": ("chick'n",),
    "jack in the box": ("jack in the box",),
    "carl's jr": ("carl's jr", "carls jr"),
    "wawa": ("wawa",),
    "sonic": ("sonic drive-in", "sonic drive in"),
    "arby's": ("arby's", "arbys"),
    "qdoba": ("qdoba",),
    "noodles & company": ("noodles & company", "noodles and company"),
    "cheesecake factory": ("cheesecake factory",),
    "olive garden": ("olive garden",),
    "applebee's": ("applebee's", "applebees"),
    "chili's": ("chili's", "chilis"),
    "buffalo wild wings": ("buffalo wild wings", "bdubs", "b-dubs"),
    "dutch bros": ("dutch bros",),
    "peet's": ("peet's", "peets"),
    "philz": ("philz",),
    "blue bottle": ("blue bottle",),
    "boba guys": ("boba guys",),
    "sharetea": ("sharetea",),
    "tendergreens": ("tendergreens", "tender greens"),
    "mendocino farms": ("mendocino farms",),
    "urban plates": ("urban plates",),
    "trader joe's": ("trader joe's", "trader joes", "tj's", "tjs"),
    "costco": ("costco", "kirkland"),
    "whole foods": ("whole foods",),
    "safeway": ("safeway",),
    # berkeley / oakland venues people name (no scraped menu)
    "free speech movement cafe": ("free speech cafe", "free speech movement cafe", "fsm cafe"),
    "golden bear cafe": ("golden bear cafe", "gbc"),
    "top dog": ("top dog",),
    "sliver": ("sliver pizza", "sliver"),
    "cheese board": ("cheese board", "cheeseboard"),
    "la burrita": ("la burrita",),
    "tacos sinaloa": ("tacos sinaloa",),
    "imm thai": ("imm thai",),
    "thai basil": ("thai basil",),
    "gypsy's": ("gypsy's", "gypsys"),
    "artichoke basille's": ("artichoke basille", "artichoke pizza"),
    "hotboys": ("hotboys", "hot boys"),
    "gara": ("gara sushi",),
    "grizzly chefs": ("grizzly chefs", "grizzly chef"),
    # packaged / drinks with a printed panel the brand also posts online
    "premier protein": ("premier protein",),
    "fairlife": ("fairlife", "core power"),
    "quest": ("quest bar", "quest chips", "quest protein"),
    "clif": ("clif bar", "clif builder"),
    "kind": ("kind bar",),
    "rxbar": ("rxbar", "rx bar"),
    "chobani": ("chobani",),
    "oikos": ("oikos",),
    "siggi's": ("siggi's", "siggis"),
    "yasso": ("yasso",),
    "halo top": ("halo top",),
    "celsius": ("celsius",),
    "monster": ("monster energy",),
    "red bull": ("red bull", "redbull"),
    "prime": ("prime hydration", "prime drink", "prime energy"),
    "gatorade": ("gatorade",),
    "alani": ("alani",),
    "ghost": ("ghost energy",),
    "muscle milk": ("muscle milk",),
    "pop-tarts": ("pop-tart", "pop tart", "poptart"),
    "oreo": ("oreo", "oreos"),
    "rice krispies treats": ("rice krispies treat", "rice krispie treat", "rice krispies treats", "rice krispie treats"),
    "nature valley": ("nature valley",),
    "cheez-it": ("cheez-it", "cheez it", "cheezit"),
    "goldfish": ("goldfish crackers", "goldfish"),
    "maruchan": ("maruchan",),
    "nissin": ("nissin", "cup noodles", "cup noodle"),
    "takis": ("takis",),
    "lunchables": ("lunchable", "lunchables"),
    "ben & jerry's": ("ben & jerry's", "ben and jerry's", "ben & jerrys", "ben and jerrys"),
    "huel": ("huel",),
    "soylent": ("soylent",),
    "liquid iv": ("liquid iv", "liquid i.v."),
    "owyn": ("owyn",),
    "orgain": ("orgain",),
}

# A description that already says where its numbers came from needs no nudge.
_SOURCE_TAG_RE = re.compile(
    r"\b(official|label|labeled|labelled|menu|usda|nutrition (sheet|facts|info|page|pdf)|"
    r"per (their|the) (site|website|sheet|panel)|their (site|website|sheet)|posted|per package|"
    r"package (says|panel|label))\b", re.I)


def _extra() -> list[str]:
    return [x.strip() for x in os.getenv("NAMED_BRANDS_EXTRA", "").split(",") if x.strip()]


def _pattern(alias: str) -> re.Pattern:
    # word boundaries that tolerate the alias ending in a non-word char ("dunkin'")
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])", re.I)


_COMPILED: list[tuple[str, re.Pattern]] | None = None


def _compiled() -> list[tuple[str, re.Pattern]]:
    global _COMPILED
    if _COMPILED is None:
        pairs = []
        for name, aliases in BRANDS.items():
            for a in aliases:
                pairs.append((name, _pattern(a)))
        for a in _extra():
            pairs.append((a.lower(), _pattern(a)))
        # longest alias first so "insomnia cookies" wins over "insomnia" in the report
        pairs.sort(key=lambda p: -len(p[1].pattern))
        _COMPILED = pairs
    return _COMPILED


def names_brand(text: str | None) -> str | None:
    """The brand / chain / venue a meal description names, or None. Dining halls with a
    scraped menu are never returned (they have their own tool)."""
    if not text:
        return None
    low = text.lower()
    try:
        from dining_scraper import detect_halls
        if detect_halls(low):
            return None
    except Exception:  # the detector is a convenience here, never a dependency
        pass
    for name, pat in _compiled():
        if pat.search(low):
            return name
    return None


def has_source_tag(text: str | None) -> bool:
    """True when the description already credits a real source (label, menu, official…)."""
    return bool(text) and bool(_SOURCE_TAG_RE.search(text))

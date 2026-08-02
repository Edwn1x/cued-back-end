"""
Macro-accuracy Phase D — USDA FoodData Central lookup (first external rung).

Mocked HTTP throughout: the response fixture mirrors the LIVE-verified shape
(INVESTIGATION §4 — foods[] with foodNutrients {nutrientId, value, unitName},
per-100g, energy id 1008 in KCAL with kJ impostors present). The failure envelope
is the point: no key / no match / timeout / 429 all degrade to clean fallthrough
answers — a meal must always be loggable, never blocked on the lookup.
"""

from __future__ import annotations

import pytest


def _usda_payload():
    """Live-verified shape, including a kJ energy row that must be ignored."""
    return {
        "totalHits": 2,
        "foods": [
            {
                "description": "Chicken breast, grilled without sauce, skin not eaten",
                "dataType": "Survey (FNDDS)", "score": 700.0,
                "foodNutrients": [
                    {"nutrientId": 1003, "nutrientName": "Protein", "value": 28.0, "unitName": "G"},
                    {"nutrientId": 1004, "nutrientName": "Total lipid (fat)", "value": 4.0, "unitName": "G"},
                    {"nutrientId": 1005, "nutrientName": "Carbohydrate, by difference", "value": 0.0, "unitName": "G"},
                    {"nutrientId": 1062, "nutrientName": "Energy", "value": 700.0, "unitName": "kJ"},
                    {"nutrientId": 1008, "nutrientName": "Energy", "value": 165, "unitName": "KCAL"},
                ],
            },
            {
                "description": "Chicken breast, grilled with sauce",
                "dataType": "Survey (FNDDS)", "score": 650.0,
                "foodNutrients": [
                    {"nutrientId": 1008, "nutrientName": "Energy", "value": 202, "unitName": "KCAL"},
                    {"nutrientId": 1003, "nutrientName": "Protein", "value": 21.2, "unitName": "G"},
                ],
            },
        ],
    }


class _FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _with_key(monkeypatch, key="test-key"):
    import config
    monkeypatch.setattr(config, "USDA_API_KEY", key)


def test_search_parses_per_100g_macros_by_nutrient_id(monkeypatch):
    import usda

    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["url"], calls["params"], calls["timeout"] = url, params, timeout
        return _FakeResp(_usda_payload())

    _with_key(monkeypatch)
    monkeypatch.setattr(usda.requests, "get", fake_get)
    results = usda.search_usda("grilled chicken breast")

    assert calls["url"].startswith("https://api.nal.usda.gov/fdc/v1/foods/search")
    assert calls["params"]["query"] == "grilled chicken breast"
    assert "Branded" not in calls["params"]["dataType"], \
        "branded long tail is the web rung, not USDA's"
    assert calls["timeout"] is not None, "an SMS-path HTTP call must carry a timeout"

    assert len(results) == 2
    top = results[0]
    assert top["description"].startswith("Chicken breast, grilled without")
    assert top["calories"] == 165, "energy must come from id 1008 KCAL, not the kJ row"
    assert top["protein_g"] == 28.0 and top["fat_g"] == 4.0 and top["carbs_g"] == 0.0
    assert results[1]["fat_g"] is None, "absent nutrients stay None, never invented"


def test_no_match_and_handler_fallthrough(db, monkeypatch):
    import usda
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    _with_key(monkeypatch)
    monkeypatch.setattr(usda.requests, "get",
                        lambda *a, **k: _FakeResp({"totalHits": 0, "foods": []}))
    user = make_user(db)
    out = dispatch_tool("usda_food_lookup", {"query": "unicorn stew"}, user.id)
    assert "no usda match" in out and "estimate normally" in out


def test_match_formats_per_100g_for_the_model(db, monkeypatch):
    import usda
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    _with_key(monkeypatch)
    monkeypatch.setattr(usda.requests, "get", lambda *a, **k: _FakeResp(_usda_payload()))
    user = make_user(db)
    out = dispatch_tool("usda_food_lookup", {"query": "grilled chicken breast"}, user.id)
    assert out.startswith("ok:")
    assert "per 100g" in out and "165" in out and "28" in out


def test_timeout_degrades_never_raises(db, monkeypatch):
    import requests
    import usda
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    def boom(*a, **k):
        raise requests.Timeout("read timeout")

    _with_key(monkeypatch)
    monkeypatch.setattr(usda.requests, "get", boom)
    user = make_user(db)
    out = dispatch_tool("usda_food_lookup", {"query": "grilled chicken"}, user.id)
    assert "unavailable" in out and "estimate normally" in out


def test_rate_limit_429_degrades_gracefully(db, monkeypatch):
    import usda
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    _with_key(monkeypatch)
    monkeypatch.setattr(usda.requests, "get", lambda *a, **k: _FakeResp(status=429))
    user = make_user(db)
    out = dispatch_tool("usda_food_lookup", {"query": "grilled chicken"}, user.id)
    assert "unavailable" in out and "estimate normally" in out


def test_missing_key_means_no_http_call_at_all(db, monkeypatch):
    import usda
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    def forbidden(*a, **k):
        raise AssertionError("HTTP attempted without an API key")

    _with_key(monkeypatch, key="")
    monkeypatch.setattr(usda.requests, "get", forbidden)
    user = make_user(db)
    out = dispatch_tool("usda_food_lookup", {"query": "grilled chicken"}, user.id)
    assert "not configured" in out and "estimate normally" in out


def test_loop_offers_tool_iff_flag_on(db, monkeypatch, anthropic_stub):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    captured = {}

    def handler(kw):
        captured["tools"] = [t.get("name") for t in kw.get("tools", [])]
        return "sounds good"

    anthropic_stub.reply_with(handler)
    user = make_user(db)

    monkeypatch.setattr(config, "USDA_LOOKUP_TOOL_ENABLED", True)
    run_agent_loop(user, "had some grilled chicken", "freeform")
    assert "usda_food_lookup" in captured["tools"]

    monkeypatch.setattr(config, "USDA_LOOKUP_TOOL_ENABLED", False)
    run_agent_loop(user, "had some grilled chicken", "freeform")
    assert "usda_food_lookup" not in captured["tools"]

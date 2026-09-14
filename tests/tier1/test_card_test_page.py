"""Workout card Phase 0: the static smoke page the founder sends to himself as a
live mini-app card to learn what a phone without the extension shows (GATE 0)."""


def test_card_test_page_is_plain_html_no_js_no_auth(client):
    r = client.get("/card/test")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("text/html")
    assert r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert "bench 185 &times; 5" in html
    assert html.count('type="checkbox"') == 4
    assert "<script" not in html
    assert "width: 300px" in html and "color-scheme" in html


# ─── Flask → sidecar card client + the Phase 0 driver ───────────────────────

import json
import pytest

SECRET = "test-internal-secret"


@pytest.fixture
def sidecar_cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def test_send_card_and_update_card_post_the_right_payloads(sidecar_cfg, monkeypatch):
    import photon_cards
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        if url.endswith("/send-card"):
            return _Resp(200, {"ok": True, "provider_message_id": "photon-card-1",
                               "card_session": {"id": "photon-card-1", "miniAppCardSession": {"sessionId": "s"}}})
        return _Resp(200, {"ok": True})
    monkeypatch.setattr(photon_cards.requests, "post", fake_post)

    r = photon_cards.send_card("+12094205037", "https://web/card/test", live=True)
    assert r == {"provider_message_id": "photon-card-1", "card_session": {"id": "photon-card-1", "miniAppCardSession": {"sessionId": "s"}}}
    photon_cards.update_card("+12094205037", r["card_session"], "https://web/card/test?v=2")
    assert calls[0][0] == "http://sidecar.test:8080/send-card" and calls[0][1] == {"phone": "+12094205037", "url": "https://web/card/test", "live": True}
    assert calls[1][0] == "http://sidecar.test:8080/update-card" and calls[1][1]["card_session"]["id"] == "photon-card-1"
    assert all(c[2] == {"X-Internal-Secret": SECRET} for c in calls)


def test_card_client_raises_on_refusal_with_photons_text(sidecar_cfg, monkeypatch):
    import photon_cards
    monkeypatch.setattr(photon_cards.requests, "post",
                        lambda *a, **k: _Resp(502, {"ok": False, "error": "mini apps require the Business plan"}))
    with pytest.raises(photon_cards.CardError, match="Business plan"):
        photon_cards.send_card("+1555", "https://web/card/test")


def test_internal_card_test_driver_is_secret_gated_and_proxies(client, sidecar_cfg, monkeypatch):
    import photon_cards
    seen = {}
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True: seen.update(send=(phone, url, live)) or
                        {"provider_message_id": "photon-card-1", "card_session": {"id": "photon-card-1"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda phone, cs, url: seen.update(update=(phone, cs["id"], url)))

    assert client.post("/internal/card-test", data=json.dumps({"phone": "+12094205037"}),
                       content_type="application/json").status_code == 401
    r = client.post("/internal/card-test", data=json.dumps({"phone": "+12094205037", "action": "send"}),
                    headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200 and r.get_json()["provider_message_id"] == "photon-card-1"
    assert seen["send"][1].endswith("/card/test") and seen["send"][1].startswith("https://") and seen["send"][2] is True
    r = client.post("/internal/card-test", data=json.dumps({"phone": "+12094205037", "action": "update",
                                                             "card_session": {"id": "photon-card-1"}, "v": 2}),
                    headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200 and seen["update"][1] == "photon-card-1" and seen["update"][2].endswith("/card/test?v=2")
    r = client.post("/internal/card-test", data=json.dumps({"phone": "+12094205037", "action": "update"}),
                    headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 400


def test_internal_card_test_driver_surfaces_a_refusal_as_502(client, sidecar_cfg, monkeypatch):
    import photon_cards

    def _refuse(phone, url, live=True):
        raise photon_cards.CardError("sidecar /send-card 502: mini apps require the Business plan")
    monkeypatch.setattr(photon_cards, "send_card", _refuse)
    r = client.post("/internal/card-test", data=json.dumps({"phone": "+12094205037"}),
                    headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 502 and "Business plan" in r.get_json()["error"]

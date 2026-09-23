"""Receipt extraction truncation fix. A full week's grocery receipt itemizes to a
long JSON list; the old max_tokens=1500 cap cut it mid-string → unparseable JSON →
a misleading 'couldn't read the photo' reply that looped when the user resent it
(live incident, user 31, 2026-09-22). Now: a bigger configurable cap, stop_reason
gated BEFORE parse, and distinct honest copy for too-long vs unreadable."""
from __future__ import annotations

import json

import pytest

import config
import receipts

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j/AAAA"}}


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 10,
                                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})()


class _FakeClient:
    """Records the create() kwargs and returns a canned response."""
    def __init__(self, resp):
        self._resp = resp
        self.calls = []
        self.messages = type("M", (), {"create": self._create})()

    def _create(self, **kw):
        self.calls.append(kw)
        return self._resp


@pytest.fixture
def stub_client(monkeypatch):
    def _install(resp):
        fc = _FakeClient(resp)
        monkeypatch.setattr(receipts, "_cl", lambda: fc)
        return fc
    return _install


VALID = json.dumps({"store": "trader joe's",
                    "items": [{"name": "eggs", "qty": 1, "unit": "dozen", "is_food": True, "est_grams": 600}]})


def test_valid_extract_returns_dict(stub_client):
    stub_client(_Resp(VALID, stop_reason="end_turn"))
    data = receipts.extract_receipt(IMG, user_id=1)
    assert isinstance(data, dict) and data["store"] == "trader joe's"


def test_max_tokens_stop_returns_truncated_sentinel_even_if_text_parses(stub_client):
    # Truncated stop must win over a coincidentally-parseable body: the item list is
    # incomplete, so we must NOT ingest a partial receipt.
    stub_client(_Resp(VALID, stop_reason="max_tokens"))
    assert receipts.extract_receipt(IMG, user_id=1) is receipts.TRUNCATED


def test_unparseable_body_returns_none(stub_client):
    stub_client(_Resp('{"store": "tj", "items": [{"name": "eg', stop_reason="end_turn"))
    assert receipts.extract_receipt(IMG, user_id=1) is None


def test_extractor_uses_configured_cap(stub_client, monkeypatch):
    monkeypatch.setattr(config, "RECEIPT_EXTRACTOR_MAX_TOKENS", 8000)
    fc = stub_client(_Resp(VALID))
    receipts.extract_receipt(IMG, user_id=1)
    assert fc.calls[0]["max_tokens"] == 8000     # not the old 1500


# ─── handle_receipt_image: distinct, non-looping copy per failure mode ────────

@pytest.fixture
def receipts_on(monkeypatch):
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", True)
    monkeypatch.setattr(receipts, "classify_image", lambda img, user_id=None: "receipt")


def test_truncation_asks_for_items_not_a_better_photo(db, receipts_on, monkeypatch):
    from tests.factories import make_user
    user = make_user(db, name="Nau")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: receipts.TRUNCATED)
    reply = receipts.handle_receipt_image(user.id, IMG)
    assert "long receipt" in reply and "protein" in reply
    assert "shot" not in reply and "photo" not in reply and "brighter" not in reply   # never blame the image

def test_unreadable_copy_differs_from_truncation(db, receipts_on, monkeypatch):
    from tests.factories import make_user
    user = make_user(db, name="Nau")
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: None)
    reply = receipts.handle_receipt_image(user.id, IMG)
    assert "type the main things" in reply
    # the two failure modes must not emit the SAME line (the looping-copy bug)
    monkeypatch.setattr(receipts, "extract_receipt", lambda img, user_id=None: receipts.TRUNCATED)
    assert receipts.handle_receipt_image(user.id, IMG) != reply

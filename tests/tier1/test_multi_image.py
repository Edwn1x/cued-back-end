"""Multi-image inbound: a user can send several photos at once (product front +
nutrition label, or a few dishes) and the model must see ALL of them, not just the
first. image_data stays the PRIMARY (first) image for single-image signals; a list
carries the rest to the vision call. Flag-gated (MULTI_IMAGE_ENABLED) + capped."""
from __future__ import annotations

import io
import json

import pytest

from tests.factories import make_user

SECRET = "testsecret"
IMG = lambda tag: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tag}}


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.local")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    # Multi-image lives on the agent-loop path (the prod path); legacy fallback stays
    # single-image, so the pipeline tests must run the agent loop.
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)


def _payload(phone, text, **extra):
    return {"phone": phone, "text": text, "provider_message_id": "photon-in-1",
            "chat_guid": f"any;-;{phone}", "service": "iMessage", "line_phone": "+15102646604",
            "timestamp": "2026-09-10T12:00:00.000Z", "attachments": [], **extra}


def _post_files(client, phone, text, names):
    data = {"payload": json.dumps(_payload(phone, text))}
    for i, n in enumerate(names):
        data[f"attachment_{i}"] = (io.BytesIO(b"\xff\xd8\xff\xe0" + bytes([i])), n, "image/jpeg")
    return client.post("/internal/inbound", data=data, headers={"X-Internal-Secret": SECRET},
                       content_type="multipart/form-data")


def _vision_capture(store):
    """Record ONLY the model turn that carries image blocks — the full pipeline also
    fires text-only extraction calls (memory/coaching points) that would otherwise
    clobber a naive last-call capture."""
    def handler(kw):
        msgs = kw.get("messages") or []
        content = msgs[0].get("content") if msgs else None
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
            store["messages"] = msgs
        return "ok"
    return handler


def _model_images(captured):
    content = captured["messages"][0]["content"]
    assert isinstance(content, list), "vision turn must send a content list"
    return [b for b in content if isinstance(b, dict) and b.get("type") == "image"]


# ─── run_agent_loop: the model sees every image ──────────────────────────────

def test_run_agent_loop_sends_all_images_and_the_caption(db, monkeypatch, anthropic_stub):
    import config
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    captured = {}
    anthropic_stub.reply_with(_vision_capture(captured))
    user = make_user(db)
    run_agent_loop(user, "front and the label", "freeform",
                   image_data_list=[IMG("front"), IMG("label")])
    imgs = _model_images(captured)
    assert len(imgs) == 2 and {i["source"]["data"] for i in imgs} == {"front", "label"}
    assert any(b.get("type") == "text" and "front and the label" in b.get("text", "")
               for b in captured["messages"][0]["content"])


def test_run_agent_loop_single_image_still_works(db, monkeypatch, anthropic_stub):
    import config
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    captured = {}
    anthropic_stub.reply_with(_vision_capture(captured))
    user = make_user(db)
    run_agent_loop(user, "check this", "freeform", image_data=IMG("solo"))
    assert len(_model_images(captured)) == 1


# ─── end-to-end: two attachments in one inbound reach the model together ──────

def test_two_attachments_in_one_inbound_reach_vision(db, client, imessage_on, driver, monkeypatch, anthropic_stub):
    import config, image_normalize
    monkeypatch.setattr(config, "MULTI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "MAX_INBOUND_IMAGES", 5)
    # Distinct normalized block per attachment (keyed by filename) — avoids PNG fiddliness
    # and lets us assert both survived to the model.
    monkeypatch.setattr(image_normalize, "normalize_image",
                        lambda b, m, name=None: IMG(name))
    captured = {}
    anthropic_stub.reply_with(_vision_capture(captured))
    user = make_user(db)
    r = _post_files(client, user.phone, "front + label", ["front.jpg", "label.jpg"])
    assert r.status_code == 200
    driver.flush(user.phone)
    imgs = _model_images(captured)
    assert len(imgs) == 2, "both attachments must reach the model's vision"
    assert {i["source"]["data"] for i in imgs} == {"front.jpg", "label.jpg"}


def test_cap_limits_how_many_images_reach_the_model(db, client, imessage_on, driver, monkeypatch, anthropic_stub):
    import config, image_normalize
    monkeypatch.setattr(config, "MULTI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "MAX_INBOUND_IMAGES", 2)
    monkeypatch.setattr(image_normalize, "normalize_image", lambda b, m, name=None: IMG(name))
    captured = {}
    anthropic_stub.reply_with(_vision_capture(captured))
    user = make_user(db)
    _post_files(client, user.phone, "a bunch", ["1.jpg", "2.jpg", "3.jpg", "4.jpg"])
    driver.flush(user.phone)
    assert len(_model_images(captured)) == 2, "cap must bound how many images are sent"


def test_flag_off_falls_back_to_first_image_only(db, client, imessage_on, driver, monkeypatch, anthropic_stub):
    import config, image_normalize
    monkeypatch.setattr(config, "MULTI_IMAGE_ENABLED", False)
    monkeypatch.setattr(image_normalize, "normalize_image", lambda b, m, name=None: IMG(name))
    captured = {}
    anthropic_stub.reply_with(_vision_capture(captured))
    user = make_user(db)
    _post_files(client, user.phone, "two pics", ["a.jpg", "b.jpg"])
    driver.flush(user.phone)
    imgs = _model_images(captured)
    assert len(imgs) == 1 and imgs[0]["source"]["data"] == "a.jpg", "flag off = first image only (legacy)"

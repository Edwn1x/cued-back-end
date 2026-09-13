"""
Inbound image normalization (image_normalize.py). Live 2026-09-13: an iPhone
camera photo arrived over iMessage as HEIC, was forwarded to Anthropic with
media_type=image/heic, and every layer re-threw the API's 400 — the founder got
"Something went wrong on my end". The format must be decided from the bytes.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _jpeg(size=(40, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _heic(size=(64, 48)):
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format="HEIF")
    return buf.getvalue()


def _decoded(block):
    return base64.b64decode(block["source"]["data"])


def test_sniff_reads_the_container_not_the_header():
    from image_normalize import sniff
    assert sniff(_jpeg()) == "image/jpeg"
    assert sniff(PNG_1PX) == "image/png"
    assert sniff(_heic()) == "image/heic"
    assert sniff(b"GIF89a" + b"\x00" * 20) == "image/gif"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8) == "video/mp4"
    assert sniff(b"not an image at all, sorry") is None


def test_heic_becomes_a_jpeg_block_the_api_accepts():
    from image_normalize import normalize_image, sniff
    block = normalize_image(_heic(), "image/heic", user_id=27, name="IMG_4242.HEIC")
    assert block is not None
    assert block["source"]["media_type"] == "image/jpeg"
    assert sniff(_decoded(block)) == "image/jpeg"


def test_mislabeled_jpeg_gets_the_real_media_type_without_reencoding():
    from image_normalize import normalize_image
    raw = _jpeg()
    block = normalize_image(raw, "image/jpg")   # Twilio-style sloppy header
    assert block["source"]["media_type"] == "image/jpeg"
    assert _decoded(block) == raw                # passthrough, byte-identical


def test_png_passes_through_untouched():
    from image_normalize import normalize_image
    block = normalize_image(PNG_1PX, "image/png")
    assert block["source"]["media_type"] == "image/png"
    assert _decoded(block) == PNG_1PX


def test_big_supported_image_passes_through_when_under_the_byte_cap():
    """A wide-but-small jpeg is fine as-is: the API downsamples, we don't re-encode."""
    from image_normalize import normalize_image, MAX_SIDE
    raw = _jpeg(size=(MAX_SIDE + 1000, 200))
    block = normalize_image(raw, "image/jpeg")
    assert _decoded(block) == raw


def test_over_the_byte_cap_is_downscaled_under_it():
    import os
    from PIL import Image
    from image_normalize import normalize_image, MAX_BYTES, MAX_SIDE
    side = 2600  # incompressible noise at q=100 → well over the 4.5MB cap
    noisy = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buf = io.BytesIO(); noisy.save(buf, format="JPEG", quality=100)
    raw = buf.getvalue()
    assert len(raw) > MAX_BYTES, "fixture must exceed the cap to prove anything"
    block = normalize_image(raw, "image/jpeg")
    out = _decoded(block)
    im = Image.open(io.BytesIO(out))
    assert len(out) <= MAX_BYTES and max(im.size) <= MAX_SIDE
    assert block["source"]["media_type"] == "image/jpeg"


def test_garbage_and_video_return_none_never_raise():
    from image_normalize import normalize_image
    assert normalize_image(b"definitely not a picture", "image/heic") is None
    assert normalize_image(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "video/mp4") is None
    assert normalize_image(b"", "image/png") is None


# ─── end to end through /internal/inbound ───────────────────────────────────

SECRET = "test-internal-secret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


def _post(client, phone, name, mime, content):
    payload = {"phone": phone, "text": "", "provider_message_id": f"photon-{name}",
               "chat_guid": f"any;-;{phone}", "service": "iMessage", "line_phone": "+15102646604",
               "timestamp": "2026-09-13T22:41:25.000Z",
               "attachments": [{"name": name, "mime_type": mime, "size": len(content)}]}
    data = {"payload": json.dumps(payload), "attachment_0": (io.BytesIO(content), name, mime)}
    return client.post("/internal/inbound", data=data, headers={"X-Internal-Secret": SECRET},
                       content_type="multipart/form-data")


def test_inbound_heic_reaches_the_pipeline_as_jpeg(db, client, imessage_on, monkeypatch):
    import app as appmod
    from tests.factories import make_user
    from sms import IMAGE_MARKER
    seen = {}

    def fake_process(session, user, from_number, body, message_sid, image_url, image_data, **kw):
        seen.update(image_url=image_url, image_data=image_data)
        return appmod.get_twiml_response(), 200, {"Content-Type": "text/xml"}
    monkeypatch.setattr(appmod, "_process_inbound", fake_process)

    user = make_user(db)
    r = _post(client, user.phone, "IMG_4242.HEIC", "image/heic", _heic())
    assert r.status_code == 200
    assert seen["image_url"] == "IMG_4242.HEIC"                    # has-image signal intact
    assert seen["image_data"]["source"]["media_type"] == "image/jpeg"


def test_inbound_unreadable_attachment_is_text_only_but_still_marked(db, client, imessage_on, monkeypatch):
    import app as appmod
    from tests.factories import make_user
    seen = {}

    def fake_process(session, user, from_number, body, message_sid, image_url, image_data, **kw):
        seen.update(image_url=image_url, image_data=image_data)
        return appmod.get_twiml_response(), 200, {"Content-Type": "text/xml"}
    monkeypatch.setattr(appmod, "_process_inbound", fake_process)

    user = make_user(db)
    r = _post(client, user.phone, "clip.mov", "video/quicktime", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 40)
    assert r.status_code == 200
    assert seen["image_url"] == "clip.mov"     # the marker still records that media came
    assert seen["image_data"] is None          # but no block goes to the model

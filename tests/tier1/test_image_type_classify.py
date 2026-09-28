"""
Neutral inbound image type (NEUTRAL_IMAGE_TYPE_ENABLED). Live 2026-09-27: the founder
sent a photo of an EZ curl bar (gym equipment) and classify_message blanket-tagged it
"food_photo" — the old default assumption for ANY image. That label only stamps the
outbound reply row (admin view + analytics); no food/meal path gates on it. The meal
estimation prompt, the receipt pre-classifier, meal logging (log_meal) and the photo
buffer band all key on image PRESENCE and the in-loop vision model, never on
message_type. So the fix is: an image with no explicit food caption gets a neutral
"image" label and the agent loop (which actually sees the photo) decides if it's food.

These pins: (1) a non-food / captionless image is NOT tagged food_photo; (2) a real
food caption still is; (3) the flag fails safe to the old default; (4) all images still
get the longer photo buffer band; (5) a food photo still logs as a meal even under the
neutral label — proving the meal path never depended on food_photo.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}

SECRET = "test-internal-secret"


# ─── 1. the classifier no longer presumes food ───────────────────────────────

def test_bare_image_is_neutral_not_food(monkeypatch):
    """A captionless image → neutral "image", never food_photo (the EZ-curl-bar case)."""
    import config
    from app import classify_message
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", True)
    assert classify_message("", has_image=True) == "image"


@pytest.mark.parametrize("caption", [
    "the ez curl bar i got",   # gym equipment — the live case
    "look at this",            # generic
    "my new shoes",            # non-food object
    "random pic",              # no signal at all
])
def test_non_food_caption_image_is_neutral(monkeypatch, caption):
    import config
    from app import classify_message
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", True)
    assert classify_message(caption, has_image=True) == "image"


@pytest.mark.parametrize("caption", [
    "here's my lunch",
    "ate this",
    "dinner tonight",
    "my breakfast",
    "a snack",
])
def test_food_caption_image_still_food_photo(monkeypatch, caption):
    """A real food caption is a genuine food SIGNAL — still labeled food_photo."""
    import config
    from app import classify_message
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", True)
    assert classify_message(caption, has_image=True) == "food_photo"


def test_flag_off_falls_back_to_the_old_food_photo_default(monkeypatch):
    """Fail-safe: with the flag off, a bare image is the legacy food_photo default."""
    import config
    from app import classify_message
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", False)
    assert classify_message("", has_image=True) == "food_photo"


# ─── 2. end to end: a non-food image is not labeled food, but keeps the photo band ──

@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


def _post_image(client, phone, name="IMG_1.png", content=PNG_1PX):
    payload = {"phone": phone, "text": "", "provider_message_id": f"photon-{name}",
               "chat_guid": f"any;-;{phone}", "service": "iMessage",
               "line_phone": "+15102646604", "timestamp": "2026-09-27T22:41:25.000Z",
               "attachments": [{"name": name, "mime_type": "image/png", "size": len(content)}]}
    data = {"payload": json.dumps(payload),
            "attachment_0": (io.BytesIO(content), name, "image/png")}
    return client.post("/internal/inbound", data=data,
                       headers={"X-Internal-Secret": SECRET},
                       content_type="multipart/form-data")


def test_non_food_image_is_labeled_neutral_and_still_gets_the_photo_band(db, client, imessage_on, monkeypatch):
    """A captionless (non-food) image through the real inbound path buffers as "image",
    NOT food_photo — and still gets the longer photo buffer band (band keys on image
    presence, not on food-ness)."""
    import app as appmod
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", True)
    seen = []
    monkeypatch.setattr(appmod, "buffer_message", lambda **kw: seen.append(kw))
    user = make_user(db, onboarding_step=3)

    _post_image(client, user.phone)

    assert seen, "the inbound should have reached buffer_message"
    assert seen[-1]["message_type"] == "image"          # NOT food_photo
    assert seen[-1]["delay_override"] == (45, 60)        # photo band preserved for ALL images


# ─── 3. a food photo still logs as a meal even under the neutral label ─────────

def test_food_photo_still_logs_as_a_meal_under_neutral_label(db, monkeypatch, anthropic_stub):
    """The meal path never depended on message_type: with a neutral "image" label the
    loop still sees the photo and logs the meal via log_meal."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)

    anthropic_stub.push(
        ToolUse("log_meal", {"description": "chicken bowl", "calories": 650,
                             "protein_g": 48, "carbs_g": 0, "fat_g": 0}),
        "logged — 650 cal, 48g protein",
    )
    user = make_user(db)
    # neutral label — exactly what classify_message now hands a bare food photo turn
    run_agent_loop(user, "here's what i ate", "image", image_data=_IMG)

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
    finally:
        s.close()
    assert len(meals) == 1 and meals[0].description == "chicken bowl"

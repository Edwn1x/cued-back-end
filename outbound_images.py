"""
outbound_images — send the user a PICTURE from a public url (the send_image tool).
=================================================================================

What was there: the only pictures the coach could send were code's own — stat cards,
the water GIF, a workout card. When the thing worth showing was somewhere on the web
(the frat's weekly menu PNG, a flyer, a map, a posted schedule, a form demo frame) the
model could at best paste a link, and voice.md bans pasted links — so it read the
picture out loud instead, or promised one and sent nothing (2026-11-02, user 77: the
Grizzly Chefs menu went out by hand).

What this is: ONE door for "put this picture in their thread". The url goes through
webfetch's envelope (public hosts only, the denylist, byte caps, PIL normalization), so
the model can only send what the coach could read; iMessage users get a real attachment
bubble via the sidecar, SMS users an MMS whose media url is the picture's own public
url (Twilio fetches it). The Message row is written with a bracketed marker so the
thread history (and the restatement guard) knows a picture went out.

Division of labor: code owns transport, caps, the per-turn cap, the repeat guard, the
row, and the honest fallback (a text with the link when the pipe refuses the bytes);
the model owns WHEN a picture is worth sending and the sentence around it.

Flag: SEND_IMAGE_TOOL_ENABLED (default off). Inert without it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.outbound_images")

SENT_MARKER_PREFIX = "[sent picture: "


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sent_recently(user_id: int, url: str, minutes: int) -> bool:
    """True when this url's picture already went to the user inside `minutes` (the model
    re-sending what is already in the thread)."""
    from models import get_session, Message
    since = _utcnow() - timedelta(minutes=max(1, minutes))
    session = get_session()
    try:
        row = (session.query(Message.id)
               .filter(Message.user_id == user_id, Message.direction == "out",
                       Message.created_at >= since,
                       Message.body.like(f"{SENT_MARKER_PREFIX}%"),
                       Message.body.like(f"%{url}%"))
               .first())
        return bool(row)
    finally:
        session.close()


def _ambiguous_dispatch(err) -> bool:
    # Photon's "dispatch may have completed": the attachment usually landed (water GIF,
    # live 2026-10-28) — count it as sent rather than double it with a text.
    return "dispatch may have completed" in str(err).lower()


def _webp_to_png(data: bytes) -> tuple[bytes, str]:
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        buf = io.BytesIO()
        (im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")).save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    except Exception as e:  # noqa: BLE001 — send the webp rather than nothing
        logger.info("IMAGE_WEBP_REENCODE_FAILED err=%s", e.__class__.__name__)
        return data, "image/webp"


def _fallback_name(url: str, caption: str) -> str:
    from webfetch import _media_name
    return _media_name(url, caption)


def send_image_url(user_id: int, url: str, *, caption: str = "") -> str:
    """Fetch `url` through webfetch (same safety envelope as reading) and send the picture
    to the user over their channel. Returns the status line for the coach loop:
    'ok: …' when it landed (or went as a link), 'error: …' with a plain reason otherwise.
    Never raises."""
    import sms
    import webfetch
    from models import get_session, User

    caption = " ".join((caption or "").split())[:120]
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.phone:
            return "error: no phone on file"
        if getattr(user, "archived_at", None):
            return "error: this account is archived"
        phone = user.phone
    finally:
        session.close()

    res = webfetch.fetch_page(url, use_cache=False)
    if not res.ok:
        return f"error: couldn't get that picture ({res.error})"
    if res.kind != "image" or not res.media:
        return ("error: that link isn't a picture (it's a page or a file) — send_image takes an image "
                "url; open the page with fetch_page and pick one of the pictures it lists")
    public_url = res.final_url or url
    if _sent_recently(user_id, public_url, config.SEND_IMAGE_REPEAT_MIN):
        return "error: that picture is already in the thread from a few minutes ago; point at it instead of resending"

    media, media_type = res.media, res.media_type
    if media_type == "image/webp":
        # Squarespace's CDN negotiates WebP; Messages renders PNG/JPEG everywhere — re-encode.
        media, media_type = _webp_to_png(media)
    name = _fallback_name(public_url, "")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(media_type, "")
    if ext and not name.lower().endswith(ext):
        name = (name.rsplit(".", 1)[0] if "." in name else name)[:60] + ext
    marker = f"{SENT_MARKER_PREFIX}{caption or name} — {public_url}]"

    channel = sms._resolve_channel(user_id)
    try:
        if channel == "imessage":
            if sms.outage_active():
                raise RuntimeError("imessage pipe is down (outage_active)")
            sid = sms._send_imessage_attachment(phone, media, name, media_type)
        else:
            sid = sms._send_single_mms(phone, public_url)
        sms._log_message(user_id, marker, "freeform", channel=channel, provider_sid=sid, delivery_status="sent")
        logger.info("IMAGE_SENT user=%s channel=%s bytes=%s type=%s %sx%s url=%r",
                    user_id, channel, len(media), media_type, res.width, res.height, public_url)
        return (f"ok: sent the picture ({res.width}x{res.height}) as its own bubble — your reply is the "
                "sentence around it, don't describe the whole picture back and don't paste the link")
    except Exception as e:  # noqa: BLE001 — the picture failing must never kill the reply
        if channel == "imessage" and _ambiguous_dispatch(e):
            try:
                sms._log_message(user_id, marker, "freeform", channel=channel, provider_sid=None,
                                 delivery_status="sent")
            except Exception as le:  # noqa: BLE001
                logger.warning("IMAGE_AMBIGUOUS_LOG_FAILED user=%s err=%s", user_id, le)
            logger.warning("IMAGE_SEND_AMBIGUOUS user=%s url=%r err=%s — counted as sent", user_id, public_url, e)
            return "ok: sent the picture as its own bubble — your reply is the sentence around it"
        logger.warning("IMAGE_SEND_FAILED user=%s channel=%s url=%r err=%s — sending the link instead",
                       user_id, channel, public_url, e)
        try:
            sms.send_sms(phone, public_url, user_id=user_id, message_type="freeform")
            return ("ok: couldn't attach the picture itself, so the LINK to it went out as its own bubble — "
                    "say it's a link to the picture, don't claim they can see it inline")
        except Exception as e2:  # noqa: BLE001
            logger.warning("IMAGE_LINK_FALLBACK_FAILED user=%s err=%s", user_id, e2)
            return "error: couldn't send the picture or a link to it right now — say so plainly"

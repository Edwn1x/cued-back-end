"""
Inbound image normalization — one seam for both transports.

Live 2026-09-13 (user 27): an iPhone camera photo arrived over iMessage as HEIC.
Flask passed the declared mime straight through as the Anthropic media_type; the
API rejected it (only jpeg/png/gif/webp) and every fallback layer re-threw the
same 400, so the founder got "Something went wrong on my end". Screenshots had
worked because they're PNG. Twilio's MMS path had the same latent hole (it trusts
the Content-Type header).

Policy — decided from the BYTES, never from the declared mime:
  - jpeg/png/gif/webp → pass through with the sniffed media_type (fixes a wrong
    or sloppy header like 'image/jpg' too), unless it's oversized (below).
  - HEIC/HEIF, or anything else Pillow can open → re-encoded as JPEG.
  - Larger than the API's per-image cap, or huge on a side → downscaled JPEG.
  - Unreadable → None. The caller then treats the turn as text-only WITH the
    stored `[image attached]` marker, so the coach says it couldn't open the
    pic (voice.md retrieval-gap honesty) instead of the pipeline dying.
"""

from __future__ import annotations

import base64
import io
import logging

logger = logging.getLogger("cued.image")

SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_BYTES = 4_500_000      # Anthropic's per-image limit is 5MB; leave headroom for base64 slop
MAX_SIDE = 2000            # px; the model downsamples past ~1568 anyway
JPEG_QUALITY = 85


def sniff(data: bytes) -> str | None:
    """Real container format from magic bytes. Returns a mime or None."""
    if not data or len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        return "video/mp4"  # an iso container that isn't a still — not an image
    return None


def _open(data: bytes):
    from PIL import Image
    try:
        import pillow_heif  # noqa: F401 — registers HEIF/AVIF openers on import
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001 — Pillow alone still handles jpeg/png/webp
        pass
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def _to_jpeg(data: bytes) -> bytes:
    from PIL import ImageOps
    im = _open(data)
    im = ImageOps.exif_transpose(im)             # camera orientation → upright pixels
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > MAX_SIDE:
        scale = MAX_SIDE / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def normalize_image(data: bytes, declared_mime: str | None = None, *, user_id=None, name=None):
    """Return an Anthropic base64 image block the API will accept, or None if the
    bytes can't be turned into one. Never raises."""
    declared = (declared_mime or "").lower().split(";")[0].strip() or "?"
    try:
        real = sniff(data)
        if real in SUPPORTED and len(data) <= MAX_BYTES:
            out, media = data, real
            how = "passthrough" if real == declared else f"header_fixed({declared}->{real})"
        elif real and real.startswith("video/"):
            logger.info("IMAGE_UNREADABLE user=%s name=%s declared=%s sniffed=%s reason=not_a_still",
                        user_id, name, declared, real)
            return None
        else:
            out, media = _to_jpeg(data), "image/jpeg"
            how = f"converted({real or declared}->jpeg)"
        logger.info("IMAGE_NORMALIZED user=%s name=%s declared=%s sniffed=%s how=%s in_bytes=%d out_bytes=%d",
                    user_id, name, declared, real, how, len(data), len(out))
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.b64encode(out).decode("utf-8")}}
    except Exception as e:  # noqa: BLE001 — an unreadable pic must not take the turn down
        logger.warning("IMAGE_UNREADABLE user=%s name=%s declared=%s bytes=%d err=%s",
                       user_id, name, declared, len(data or b""), e)
        return None

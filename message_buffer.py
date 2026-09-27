"""
Message Buffer — Cued
======================
Batches incoming messages per user with a delay before processing.
This lets users send multiple texts as one thought, and makes the
coach feel human instead of instant.
"""

import threading
import random
import logging
import time
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.buffer")

# In-memory buffer: phone_number -> {"messages": [...], "timer": Timer, "user_id": int, "token": object}
_buffers = {}
_lock = threading.Lock()

# phone -> monotonic timestamp of the last flush, so a message arriving right
# after a flush (the timer-vs-append race) can be spotted and logged.
_last_flush = {}

# Delay range in seconds (randomized to feel human)
MIN_DELAY = 90
MAX_DELAY = 150


def _get_delay():
    """Random delay between 90-150 seconds."""
    return random.randint(MIN_DELAY, MAX_DELAY)


def buffer_message(phone: str, body: str, user_id: int, message_type: str,
                   image_url: str = None, process_callback=None, delay_override: tuple = None,
                   images: list = None):
    """
    Add a message to the buffer for this phone number.
    If a timer is already running, cancel it and restart.
    When the timer expires, all buffered messages are combined
    and sent to process_callback.

    delay_override: optional (min, max) tuple in seconds to override the default 90-150s delay.
    """
    with _lock:
        if phone in _buffers:
            # Cancel existing timer
            _buffers[phone]["timer"].cancel()
            # Append new message
            _buffers[phone]["messages"].append({
                "body": body,
                "message_type": message_type,
                "image_url": image_url,
                "images": images if images else ([image_url] if image_url else []),
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
            logger.info(f"Appended to buffer for {phone} ({len(_buffers[phone]['messages'])} messages)")
        else:
            # Create new buffer entry. If this phone was flushed a heartbeat ago,
            # this message raced the timer that just fired — the previous turn is
            # already committed, so it can't join that flush. Log it; the outbound
            # dedup layer (sms._is_duplicate_send) is what stops the user from
            # seeing two near-identical replies for the split thought.
            last = _last_flush.get(phone)
            if last is not None and (time.monotonic() - last) < config.BUFFER_JOIN_WINDOW_S:
                logger.warning(
                    "BUFFER_LATE_APPEND phone=%s within=%.2fs of last flush — new turn; "
                    "outbound dedup guards the reply", phone, time.monotonic() - last)
            _buffers[phone] = {
                "messages": [{
                    "body": body,
                    "message_type": message_type,
                    "image_url": image_url,
                    "images": images if images else ([image_url] if image_url else []),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }],
                "user_id": user_id,
            }
            logger.info(f"New buffer created for {phone}")

        # Start a new timer, tagged with a unique token. The token is how a flush
        # tells "I am the current timer" from "I was superseded by a later append
        # but fired anyway because cancel() lost the race" — see _flush_buffer.
        token = object()
        _buffers[phone]["token"] = token
        delay = random.randint(delay_override[0], delay_override[1]) if delay_override else _get_delay()
        timer = threading.Timer(delay, _flush_buffer, args=[phone, process_callback, token])
        timer.daemon = True
        _buffers[phone]["timer"] = timer
        timer.start()
        logger.info(f"Timer set for {phone}: {delay}s")


def _flush_buffer(phone: str, process_callback, token=None):
    """
    Timer expired — combine all buffered messages and process them.

    `token` guards the timer-vs-append race: a late append cancels this timer and
    starts a new one, but threading.Timer.cancel() is a no-op once the timer has
    already begun firing. Without the guard that stale timer would pop and process
    the buffer, and the new timer (or a new turn) would then double-reply. So a
    flush only proceeds when its token still matches the buffer's current token;
    a superseded timer returns quietly and lets the current timer flush both
    messages as one turn.
    """
    with _lock:
        if phone not in _buffers:
            return

        if token is not None and _buffers[phone].get("token") is not token:
            logger.info("Flush skipped for %s — superseded by a later append (race guard)", phone)
            return

        buffer_data = _buffers.pop(phone)
        _last_flush[phone] = time.monotonic()

    messages = buffer_data["messages"]
    user_id = buffer_data["user_id"]

    # Combine all message bodies into one input
    combined_body = "\n".join(m["body"] for m in messages if m["body"])

    # Use the most specific message_type (prefer non-freeform)
    message_type = "freeform"
    for m in messages:
        if m["message_type"] != "freeform":
            message_type = m["message_type"]
            break

    # Combine images across every buffered message (someone firing off several photos
    # in a row → one turn that sees them all), capped. image_url stays the FIRST for
    # the single-image callback arg; images carries the whole set.
    images = []
    for m in messages:
        for img in (m.get("images") or ([m["image_url"]] if m.get("image_url") else [])):
            if img is not None and img not in images:
                images.append(img)
    if config.MULTI_IMAGE_ENABLED:
        images = images[:config.MAX_INBOUND_IMAGES]
    else:
        images = images[:1]
    image_url = images[0] if images else None

    logger.info(f"Flushing buffer for {phone}: {len(messages)} messages combined -> '{combined_body[:80]}...' images={len(images)}")

    # Call the processing function
    if process_callback:
        try:
            process_callback(user_id, combined_body, message_type, image_url, images=images)
        except Exception as e:
            logger.error(f"Error processing buffered messages for {phone}: {e}", exc_info=True)


def cancel_buffer(phone: str):
    """Cancel any pending buffer for a phone number (e.g., on STOP)."""
    with _lock:
        if phone in _buffers:
            _buffers[phone]["timer"].cancel()
            del _buffers[phone]
            logger.info(f"Buffer cancelled for {phone}")

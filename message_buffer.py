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
from datetime import datetime

logger = logging.getLogger("cued.buffer")

# In-memory buffer: phone_number -> {"messages": [...], "timer": Timer, "user_id": int}
_buffers = {}
_lock = threading.Lock()

# Delay range in seconds (randomized to feel human)
MIN_DELAY = 90
MAX_DELAY = 150


def _get_delay():
    """Random delay between 90-150 seconds."""
    return random.randint(MIN_DELAY, MAX_DELAY)


def buffer_message(phone: str, body: str, user_id: int, message_type: str,
                   image_url: str = None, process_callback=None, delay_override: tuple = None):
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
                "received_at": datetime.now().isoformat(),
            })
            logger.info(f"Appended to buffer for {phone} ({len(_buffers[phone]['messages'])} messages)")
        else:
            # Create new buffer entry
            _buffers[phone] = {
                "messages": [{
                    "body": body,
                    "message_type": message_type,
                    "image_url": image_url,
                    "received_at": datetime.now().isoformat(),
                }],
                "user_id": user_id,
            }
            logger.info(f"New buffer created for {phone}")

        # Start a new timer
        delay = random.randint(delay_override[0], delay_override[1]) if delay_override else _get_delay()
        timer = threading.Timer(delay, _flush_buffer, args=[phone, process_callback])
        timer.daemon = True
        _buffers[phone]["timer"] = timer
        timer.start()
        logger.info(f"Timer set for {phone}: {delay}s")


def _flush_buffer(phone: str, process_callback):
    """
    Timer expired — combine all buffered messages and process them.
    """
    with _lock:
        if phone not in _buffers:
            return

        buffer_data = _buffers.pop(phone)

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

    # Use the last image if any message had one
    image_url = None
    for m in messages:
        if m["image_url"]:
            image_url = m["image_url"]

    logger.info(f"Flushing buffer for {phone}: {len(messages)} messages combined -> '{combined_body[:80]}...'")

    # Call the processing function
    if process_callback:
        try:
            process_callback(user_id, combined_body, message_type, image_url)
        except Exception as e:
            logger.error(f"Error processing buffered messages for {phone}: {e}", exc_info=True)


def cancel_buffer(phone: str):
    """Cancel any pending buffer for a phone number (e.g., on STOP)."""
    with _lock:
        if phone in _buffers:
            _buffers[phone]["timer"].cancel()
            del _buffers[phone]
            logger.info(f"Buffer cancelled for {phone}")

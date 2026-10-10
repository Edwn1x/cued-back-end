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

# In-memory buffer: phone_number -> {"messages": [...], "timer": Timer, "user_id": int, "token": object, "band": (lo, hi)}
_buffers = {}
_lock = threading.Lock()

# phone -> monotonic timestamp of the last flush, so a message arriving right
# after a flush (the timer-vs-append race) can be spotted and logged.
_last_flush = {}

# phone -> threading.Event that is SET while a flushed turn is still being processed
# (the model call, the send). A text that lands in that window is a CONTINUATION of
# the thought the coach is already answering (founder, live 2026-10-05: "Lwk a combo
# of both" / "I'm either walking somewhere or in my room", 2 texts → 2 replies asking
# the same thing). Its turn is marked continuation=True and its flush waits for the
# in-flight turn to finish so history is in order when the model sees it.
_in_flight: dict = {}

# Delay range in seconds (randomized to feel human)
MIN_DELAY = 90
MAX_DELAY = 150


def _get_delay():
    """Random delay between 90-150 seconds."""
    return random.randint(MIN_DELAY, MAX_DELAY)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mark_pending(phone: str, user_id: int, has_image: bool, *, first: bool) -> None:
    """Persist 'a turn is pending for this phone' (inbound_recovery reads it after a deploy).
    `first` = the buffer was just created: the latest inbound row is the turn's first.
    Fail-open — the buffer never depends on this write."""
    if not config.INBOUND_RECOVERY_ENABLED:
        return
    try:
        from models import get_session, InboundPending, Message
        s = get_session()
        try:
            row = s.get(InboundPending, phone)
            if row is None:
                row = InboundPending(phone=phone, user_id=user_id, has_image=False, created_at=_utcnow())
                s.add(row)
            if first or row.first_message_id is None:
                latest = (s.query(Message.id).filter(Message.user_id == user_id, Message.direction == "in")
                          .order_by(Message.id.desc()).first())
                row.first_message_id = latest[0] if latest else None
            row.user_id = user_id
            row.has_image = bool(row.has_image) or bool(has_image)
            row.updated_at = _utcnow()
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("INBOUND_PENDING_MARK_FAILED phone=%s err=%s", phone, e)


def _clear_pending(phone: str) -> None:
    if not config.INBOUND_RECOVERY_ENABLED:
        return
    try:
        from models import get_session, InboundPending
        s = get_session()
        try:
            row = s.get(InboundPending, phone)
            if row is not None:
                s.delete(row)
                s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("INBOUND_PENDING_CLEAR_FAILED phone=%s err=%s", phone, e)


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
    has_image = bool(images or image_url)
    with _lock:
        if phone in _buffers:
            # Cancel existing timer
            _buffers[phone]["timer"].cancel()
            pending = _buffers[phone]["messages"]
            pending_has_image = any(m.get("images") or m.get("image_url") for m in pending)
            # FOLD (live 2026-10-02 13:16 ×3/24h): a photo landing while a text turn is
            # still pending joins THAT turn and extends it to the photo band — the
            # caption, or a trailing "also log this", usually follows the pic — so one
            # model call sees the texts and the image and there is ONE reply, instead
            # of the text turn flushing on its short band and the photo restating the
            # outcome as a second turn. A text joining a pending photo already folds
            # (the caption arrived; its own band applies). The per-timer token below
            # is untouched: a flush that has already started keeps its turn.
            fold = None
            if has_image and not pending_has_image and config.INBOUND_FOLD_PHOTO_INTO_PENDING_TEXT:
                base = delay_override or (MIN_DELAY, MAX_DELAY)
                delay_override = (max(base[0], config.PHOTO_BUFFER_S[0]),
                                  max(base[1], config.PHOTO_BUFFER_S[1]))
                fold = "photo_into_text"
            elif not has_image and pending_has_image:
                fold = "text_into_photo"
            # Append new message
            pending.append({
                "body": body,
                "message_type": message_type,
                "image_url": image_url,
                "images": images if images else ([image_url] if image_url else []),
                "received_at": datetime.now(timezone.utc).isoformat(),
            })
            if fold:
                logger.info("BUFFER_FOLD phone=%s kind=%s messages=%d band=%s",
                            phone, fold, len(pending), delay_override)
            logger.info(f"Appended to buffer for {phone} ({len(pending)} messages)")
            _mark_pending(phone, user_id, has_image, first=False)
        else:
            # Create new buffer entry. If this phone was flushed a heartbeat ago,
            # this message raced the timer that just fired — the previous turn is
            # already committed, so it can't join that flush. Log it; the outbound
            # dedup layer (sms._is_duplicate_send) is what stops the user from
            # seeing two near-identical replies for the split thought.
            last = _last_flush.get(phone)
            late = last is not None and (time.monotonic() - last) < config.BUFFER_JOIN_WINDOW_S
            busy = _in_flight.get(phone)
            in_flight = bool(busy is not None and not busy.is_set())
            if late:
                logger.warning(
                    "BUFFER_LATE_APPEND phone=%s within=%.2fs of last flush — new turn; "
                    "outbound dedup guards the reply", phone, time.monotonic() - last)
            continuation = bool(getattr(config, "ONBOARDING_CONTINUATION_FOLD_ENABLED", True)
                                and not has_image and (late or in_flight))
            _buffers[phone] = {
                "messages": [{
                    "body": body,
                    "message_type": message_type,
                    "image_url": image_url,
                    "images": images if images else ([image_url] if image_url else []),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }],
                "user_id": user_id,
                "continuation": continuation,
            }
            if continuation:
                logger.info("BUFFER_CONTINUATION phone=%s in_flight=%s late=%s", phone, in_flight, late)
            logger.info(f"New buffer created for {phone}")
            _mark_pending(phone, user_id, has_image, first=True)

        # Start a new timer, tagged with a unique token. The token is how a flush
        # tells "I am the current timer" from "I was superseded by a later append
        # but fired anyway because cancel() lost the race" — see _flush_buffer.
        token = object()
        _buffers[phone]["token"] = token
        _buffers[phone]["callback"] = process_callback      # drain_all needs it without the timer
        delay = random.randint(delay_override[0], delay_override[1]) if delay_override else _get_delay()
        _buffers[phone]["band"] = delay_override or (MIN_DELAY, MAX_DELAY)
        timer = threading.Timer(delay, _flush_buffer, args=[phone, process_callback, token])
        timer.daemon = True
        _buffers[phone]["timer"] = timer
        timer.start()
        logger.info(f"Timer set for {phone}: {delay}s")


def _takes_continuation(fn) -> bool:
    try:
        import inspect
        params = inspect.signature(fn).parameters
        return "continuation" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return False


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
        prior = _in_flight.get(phone)
        done = threading.Event()
        _in_flight[phone] = done

    messages = buffer_data["messages"]
    user_id = buffer_data["user_id"]
    continuation = bool(buffer_data.get("continuation"))

    # A continuation waits for the turn it continues (bounded): the model must see the
    # reply that already went, in order, or it answers the same thing again.
    if continuation and prior is not None and not prior.is_set():
        waited = prior.wait(getattr(config, "BUFFER_INFLIGHT_WAIT_S", 90))
        logger.info("BUFFER_CONTINUATION_WAITED phone=%s finished=%s", phone, waited)

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

    # Call the processing function. `continuation` is passed only when True and the
    # callback takes it (test callbacks and older signatures don't).
    if process_callback:
        try:
            extra = {"continuation": True} if (continuation and _takes_continuation(process_callback)) else {}
            process_callback(user_id, combined_body, message_type, image_url, images=images, **extra)
            # The inbound_pending marker is cleared only once the turn COMPLETED. Live
            # 2026-10-09 20:30 PT: the SIGTERM drain flushed "What do you think", the marker
            # was cleared at pop, the process was killed mid-reply — and the next boot had
            # nothing to replay. A turn that dies in the callback stays marked for it.
            _clear_pending(phone)
        except Exception as e:
            logger.error(f"Error processing buffered messages for {phone}: {e}", exc_info=True)
        finally:
            done.set()
            with _lock:
                if _in_flight.get(phone) is done:
                    _in_flight.pop(phone, None)
    else:
        _clear_pending(phone)        # nothing to run: the turn is consumed
        done.set()
        with _lock:
            if _in_flight.get(phone) is done:
                _in_flight.pop(phone, None)


def cancel_buffer(phone: str):
    """Cancel any pending buffer for a phone number (e.g., on STOP)."""
    with _lock:
        if phone in _buffers:
            _buffers[phone]["timer"].cancel()
            del _buffers[phone]
            logger.info(f"Buffer cancelled for {phone}")


def pending_phones() -> list[str]:
    with _lock:
        return list(_buffers.keys())


def drain_all(reason: str = "shutdown") -> int:
    """Flush every pending buffer NOW, synchronously, in this thread — the SIGTERM path.
    Each flush is the full turn (model call + send), so this is bounded only by the
    platform's grace period; whatever doesn't finish is still covered by the
    inbound_pending marker (inbound_recovery on the next boot). Returns turns flushed."""
    with _lock:
        phones = list(_buffers.keys())
        for ph in phones:
            try:
                _buffers[ph]["timer"].cancel()
            except Exception:  # noqa: BLE001
                pass
    if phones:
        logger.warning("BUFFER_DRAIN reason=%s pending=%d phones=%s", reason, len(phones), phones)
    n = 0
    for ph in phones:
        with _lock:
            entry = _buffers.get(ph)
        if not entry:
            continue
        try:
            _flush_buffer(ph, entry.get("callback"), entry.get("token"))
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.error("BUFFER_DRAIN_FLUSH_FAILED phone=%s err=%s", ph, e, exc_info=True)
    return n

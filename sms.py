import difflib
import hashlib
import logging
import re
import threading
import time

import requests
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

import config
from models import get_session, Message, User
from sms_encoding import normalize_for_sms, residual_non_gsm, estimate_segments

logger = logging.getLogger("cued.sms")

client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

# ─── Outbound dedup (buffer-race backstop) ────────────────────────────────────
# user_id -> (normalized_body, monotonic_ts) of the last reply we sent. When the
# buffer race spawns a second, topically identical turn, the coach produces a
# near-identical reply moments later; suppressing it inside OUTBOUND_DEDUP_WINDOW_S
# is what the user experiences as "not getting the same messages twice". We keep
# the normalized TEXT (not just a hash) so we can also catch PARAPHRASED
# near-duplicates, not only byte-for-byte repeats. Keyed by user_id so it spans
# both channels (iMessage + SMS).
_recent_out: dict = {}
_recent_out_lock = threading.Lock()


def _norm_body(body: str) -> str:
    """Whitespace-collapsed, lowercased — so trivial re-rendering differences
    (a stray newline, casing) still count as the same message."""
    return " ".join((body or "").split()).lower()


def _similarity(a: str, b: str) -> float:
    """Max of three cheap similarity signals on two normalized bodies, so a
    paraphrase that any one signal misses is still caught:
      • token-set Jaccard   — word overlap, order-blind
      • char SequenceMatcher ratio — edit-distance-ish, punctuation/order aware
      • token-SORT ratio     — reordered-but-same-words paraphrases
    Returns 0.0..1.0. The reported live paraphrase ("so pullups, then u alternate
    bis and back / what are the actual bi and back moves" vs "pullups, then bi/back
    alternating / what are the actual back and bicep moves u run") scores ~0.88
    here, while genuinely distinct replies sit near ~0.3 — a wide margin."""
    if not a or not b:
        return 0.0
    ta, tb = a.split(), b.split()
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb) if (sa or sb) else 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    tsort = difflib.SequenceMatcher(
        None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jac, ratio, tsort)


def _is_duplicate_send(user_id, body) -> bool:
    """True when this body duplicates the one just sent to this user inside the
    dedup window — either NORMALIZED-IDENTICAL, or (when OUTBOUND_NEAR_DEDUP_ENABLED)
    a HIGH-SIMILARITY paraphrase at/above OUTBOUND_NEAR_DEDUP_THRESHOLD. Records the
    send as a side effect so the NEXT call can see it. No-op (never suppresses)
    without a user_id or when OUTBOUND_DEDUP_ENABLED is off."""
    if not user_id or not config.OUTBOUND_DEDUP_ENABLED:
        return False
    norm = _norm_body(body)
    now = time.monotonic()
    with _recent_out_lock:
        prev = _recent_out.get(user_id)
        if prev and (now - prev[1]) < config.OUTBOUND_DEDUP_WINDOW_S:
            prev_norm = prev[0]
            if prev_norm == norm:
                return True
            if config.OUTBOUND_NEAR_DEDUP_ENABLED and prev_norm and norm:
                try:
                    if _similarity(prev_norm, norm) >= config.OUTBOUND_NEAR_DEDUP_THRESHOLD:
                        logger.info("OUTBOUND_NEAR_DEDUP user=%s suppressed near-duplicate", user_id)
                        return True
                except Exception:
                    # Fail open: a similarity error must never block a real send.
                    pass
        _recent_out[user_id] = (norm, now)
    return False


def reset_outbound_dedup():
    """Clear the dedup cache (test isolation; also safe operationally)."""
    with _recent_out_lock:
        _recent_out.clear()

SMS_SPLIT_DELAY = 2.5  # seconds between split messages
SMS_SEGMENT_WARN_THRESHOLD = 6  # ~900+ GSM-7 chars; log when bodies get this large

# Appended to the stored inbound body when the MMS carried media. The image bytes
# only ever exist inside the live turn's API call, so this marker is the ONE durable
# trace that an image arrived — it's what lets a later turn honestly say "you sent a
# pic earlier but i didn't save the detail" instead of "nothing came through".
# voice.md's retrieval-gap honesty rule references this literal string.
IMAGE_MARKER = "[image attached]"


def _send_single(phone: str, body: str) -> str:
    """Send one SMS segment via Twilio and return the SID."""
    message = client.messages.create(
        body=body,
        from_=config.TWILIO_PHONE_NUMBER,
        to=phone,
    )
    return message.sid


def _log_message(user_id: int, body: str, message_type: str,
                 channel: str = "sms", provider_sid: str | None = None,
                 delivery_status: str = "sent"):
    """Log an outbound message to the database, stamped with which pipe carried it
    and whether it landed. `delivery_status='failed'` rows are what the keystone
    (engagement_tracker.increment_unanswered) reads — write them, never skip them."""
    session = get_session()
    try:
        row = Message(
            user_id=user_id, direction="out", body=body, message_type=message_type,
            channel=channel, provider_sid=provider_sid, delivery_status=delivery_status,
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


# ─── Photon migration Phase 4A: channel router (above _send_single) ──────────
# send_sms() stays the single chokepoint; ~20 call sites are untouched. The
# router decides per-send from code-owned state (flag, sidecar configured,
# user.preferred_channel, user.channel_failed_over) — never from the model.

def _resolve_channel(user_id) -> str:
    """'imessage' only when the flag is on, a sidecar is configured, and the user
    asked for it AND their breaker is closed. Everything else is 'sms'."""
    if not user_id or not config.IMESSAGE_CHANNEL_ENABLED or not config.SIDECAR_URL:
        return "sms"
    session = get_session()
    try:
        row = (session.query(User.preferred_channel, User.channel_failed_over)
               .filter(User.id == user_id).first())
    finally:
        session.close()
    if row and row[0] == "imessage" and not row[1]:
        return "imessage"
    return "sms"


def _send_imessage(phone: str, body: str, reply_to: str = None) -> str:
    """POST {phone, text[, reply_to]} to the sidecar's /send. `reply_to` is the
    Photon id of one of THEIR messages → a threaded iMessage reply quoting it.
    Returns the Photon message id. Raises on non-2xx, on ok=false, or on any
    transport error — the caller writes the `failed` row and fails over."""
    payload = {"phone": phone, "text": body}
    if reply_to:
        payload["reply_to"] = reply_to
    resp = requests.post(
        config.SIDECAR_URL.rstrip("/") + "/send",
        json=payload,
        headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
        timeout=config.SIDECAR_TIMEOUT_S,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"sidecar /send {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"sidecar /send not ok: {resp.text[:200]}")
    return data.get("provider_message_id")


def _mark_failed_over(user_id: int):
    """Trip the user's circuit breaker: subsequent sends go straight to SMS until
    an operator clears channel_failed_over (admin). Code-owned, deliberate."""
    from datetime import datetime, timezone
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user and not user.channel_failed_over:
            user.channel_failed_over = True
            user.channel_failover_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()


def _imessage_body(body: str) -> str:
    """iMessage gets the whole message as ONE bubble: the coach's `---` part
    boundaries become blank lines (never a literal `---`), and there is no GSM-7
    normalization — iMessage is unicode."""
    parts = [p.strip() for p in re.split(r"\s*---\s*", body) if p.strip()]
    return "\n\n".join(parts) if parts else body


BUBBLE_CAP = 3
BUBBLE_MIN_DELAY = 1.0
BUBBLE_MAX_DELAY = 3.5


def split_bubbles(body: str, cap: int = BUBBLE_CAP) -> list[str]:
    """Split a coach reply into bubbles on `---`; each is one send. Up to `cap`;
    extras fold into the last (blank-line joined). Voice rewrite 2026-09-15."""
    parts = [p.strip() for p in re.split(r"\s*---\s*", body) if p.strip()]
    if len(parts) > cap:
        parts = parts[:cap - 1] + ["\n\n".join(parts[cap - 1:])]
    return parts if parts else [body]


def bubble_delay(part: str) -> float:
    """Roughly typing speed: short bubble follows fast, longer one takes a beat."""
    return max(BUBBLE_MIN_DELAY, min(BUBBLE_MAX_DELAY, len(part) / 25.0))


def split_message(body: str) -> list[str]:
    """Back-compat alias — both channels split on `---` up to BUBBLE_CAP now."""
    return split_bubbles(body)


TAPBACKS = {"love": "❤️", "like": "👍", "dislike": "👎", "laugh": "😂", "emphasize": "‼️", "question": "❓"}


def react_to_latest_inbound(user_id: int, emoji: str) -> bool:
    """Best-effort tapback on the user's newest iMessage (their last text). False when
    there is no iMessage inbound to react to or the reaction failed."""
    try:
        session = get_session()
        try:
            m = (session.query(Message.provider_sid)
                 .filter(Message.user_id == user_id, Message.direction == "in", Message.channel == "imessage",
                         Message.provider_sid.isnot(None)).order_by(Message.id.desc()).first())
        finally:
            session.close()
        if m and m[0]:
            return react_to_message(user_id, m[0], emoji)
    except Exception as e:  # noqa: BLE001
        logger.info("REACT_LATEST_SKIPPED user=%s err=%s", user_id, e)
    return False


def react_to_message(user_id: int, provider_sid: str, emoji: str) -> bool:
    """Tapback on one of the user's iMessages (by the Photon id stored on its row).
    Logs its own outbound row with message_type="reaction" so the history window
    sees it and the coach doesn't re-ack — and so every silence gate can EXCLUDE
    it (engagement_tracker._not_reaction). A failed reaction is logged and is
    neither a strike nor a channel failure (a stale message id ≠ a dead pipe):
    the breaker is NOT tripped. Returns True on success."""
    if not user_id or not provider_sid or not emoji:
        return False
    if _resolve_channel(user_id) != "imessage":
        return False
    session = get_session()
    try:
        row = session.query(User.phone).filter(User.id == user_id).first()
    finally:
        session.close()
    if not row:
        return False
    shown = TAPBACKS.get(emoji.strip().lower(), emoji.strip())
    body = f"[reacted {shown} to their message]"
    try:
        resp = requests.post(
            config.SIDECAR_URL.rstrip("/") + "/react",
            json={"phone": row[0], "message_id": provider_sid, "emoji": emoji},
            headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
            timeout=config.SIDECAR_TIMEOUT_S,
        )
        data = resp.json() if resp.status_code < 300 else {}
        if resp.status_code >= 300 or not data.get("ok"):
            raise RuntimeError(f"sidecar /react {resp.status_code}: {resp.text[:200]}")
        _log_message(user_id, body, "reaction",
                     channel="imessage", provider_sid=data.get("provider_message_id"), delivery_status="sent")
        logger.info("REACTION_SENT user_id=%s emoji=%s on=%s", user_id, shown, provider_sid)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("REACTION_FAILED user_id=%s emoji=%s on=%s err=%s — not a strike, breaker untouched",
                       user_id, shown, provider_sid, e)
        _log_message(user_id, body, "reaction",
                     channel="imessage", provider_sid=None, delivery_status="failed")
        return False


CONSENT_GATE_MARKER = "Target not allowed"


def _is_consent_gate(err) -> bool:
    """Photon's shared-pool refusal for a user who hasn't texted their line yet."""
    return CONSENT_GATE_MARKER.lower() in str(err).lower()


# ─── One number: hold on the line during a Photon outage (2026-09-28) ─────────
# An opted-in iMessage user is on ONE number — their Photon line. Live 04:44 UTC
# Photon's upstream answered "Service temporarily unavailable" for ~14 min; the
# old failover carried the reply to Twilio and tripped the breaker, so three green
# texts landed in the user's OLD thread from a different number. Now, for a user
# who has texted their line (imessage_opted_in_at), a hard first-bubble failure
# that isn't the consent gate → brief inline retries on the same line → HOLD
# (held_outbound + 'held' Message rows). held_outbound.drain delivers in order once
# Photon answers, prefixed by a light heads-up if it waited a while. The breaker
# is untouched and Twilio is never used. A user who has NOT opted in keeps the
# old path: Twilio IS the number they're on.

TRANSIENT_MARKERS = ("temporarily unavailable", "[upstream]", "not connected", "unavailable",
                     "econnrefused", "econnreset", "timed out", "timeout", "bad gateway")
REPLY_TARGET_MISSING_MARKER = "reply_to message not found"


def _is_transient_sidecar_error(err) -> bool:
    """Photon/sidecar down (retry later) vs. a request Photon rejected on purpose."""
    if isinstance(err, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    s = str(err).lower()
    if re.search(r"sidecar /send 50[234]\b", s):
        return True
    return any(m in s for m in TRANSIENT_MARKERS)


def _is_reply_target_missing(err) -> bool:
    """The threaded-reply target id is stale/unknown on the sidecar — not an outage;
    the same text sends fine as a plain bubble."""
    return REPLY_TARGET_MISSING_MARKER in str(err).lower()


_outage_lock = threading.Lock()
_outage_until = 0.0  # monotonic; > now() means "a send just failed transiently"


def note_outage():
    """A transient sidecar failure just happened: for the cooldown, new sends to
    opted-in users queue straight behind instead of each burning inline retries."""
    global _outage_until
    with _outage_lock:
        _outage_until = time.monotonic() + config.IMESSAGE_HOLD_OUTAGE_COOLDOWN_S


def clear_outage():
    global _outage_until
    with _outage_lock:
        _outage_until = 0.0


def outage_active() -> bool:
    with _outage_lock:
        return time.monotonic() < _outage_until


def _hold_eligible(user_id) -> bool:
    """Hold (never hop numbers) only for a user who is actually ON their line."""
    if not user_id or not config.IMESSAGE_HOLD_ON_OUTAGE:
        return False
    session = get_session()
    try:
        row = session.query(User.imessage_opted_in_at).filter(User.id == user_id).first()
    finally:
        session.close()
    return bool(row and row[0])


def _send_bubbles(phone: str, bubbles: list[str], reply_to_sid, user_id, message_type: str,
                  row_ids: list | None = None):
    """Send each bubble in order, threading the first on `reply_to_sid`. Returns
    (first_sid, first_err, first_err_is_read_timeout).

    Rows: a landed bubble is logged 'sent' (or, with `row_ids`, the pre-written
    'held' row at that index is flipped to sent + stamped now). A later bubble
    failing logs/flips its row and stops — the earlier bubbles landed. A first
    bubble that hard-fails writes NO row here: the caller decides whether that is
    'failed' (SMS failover) or 'held' (outage hold). A first-bubble READ timeout
    is "maybe delivered" and is logged 'sent' (send-reliability, 2026-09-26)."""
    first_sid = None
    for i, part in enumerate(bubbles):
        rid = row_ids[i] if row_ids and i < len(row_ids) else None
        try:
            sid = (_send_imessage(phone, part, reply_to_sid) if (i == 0 and reply_to_sid)
                   else _send_imessage(phone, part))
            if rid:
                _flip_row(rid, provider_sid=sid, delivery_status="sent")
            else:
                _log_message(user_id, part, message_type,
                             channel="imessage", provider_sid=sid, delivery_status="sent")
            if i == 0:
                first_sid = sid
            if i < len(bubbles) - 1:
                time.sleep(bubble_delay(part))
        except Exception as e:  # noqa: BLE001
            is_read_timeout = (config.SIDECAR_TIMEOUT_NO_FAILOVER
                               and isinstance(e, requests.exceptions.ReadTimeout))
            if i == 0 and not is_read_timeout:
                return None, e, False
            status = "sent" if is_read_timeout else "failed"
            if rid:
                _flip_row(rid, provider_sid=None, delivery_status=status)
            else:
                _log_message(user_id, part, message_type, channel="imessage",
                             provider_sid=None, delivery_status=status)
            if i == 0:
                return None, e, True
            if is_read_timeout:
                logger.warning("IMESSAGE_BUBBLE_TIMEOUT user_id=%s idx=%s — maybe delivered; "
                               "earlier bubbles landed, not failing over", user_id, i)
            else:
                logger.warning("IMESSAGE_BUBBLE_FAILED user_id=%s idx=%s err=%s — earlier bubbles landed",
                               user_id, i, e)
            return first_sid, None, False
    return first_sid, None, False


def _flip_row(row_id: int, provider_sid, delivery_status: str):
    """A held Message row just landed (or definitively didn't). Stamp created_at to
    now so the conversation window orders it where the user actually saw it."""
    from datetime import datetime, timezone
    session = get_session()
    try:
        row = session.get(Message, row_id)
        if row is None:
            return
        row.provider_sid = provider_sid
        row.delivery_status = delivery_status
        if delivery_status == "sent":
            row.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
    finally:
        session.close()


def _typing_stop(user_id):
    try:
        from typing_indicator import typing_stop
        typing_stop(user_id)
    except Exception:  # noqa: BLE001
        pass


IMESSAGE_INVITE = "ps — i can text you on iMessage instead. tap this once and say hey: {link}"


def _with_imessage_invite(user_id: int, body: str) -> str:
    """Append the one-tap opt-in link to an SMS body (onboarding hook only)."""
    try:
        from photon import imessage_link_for_user
        link = imessage_link_for_user(user_id)
    except Exception:  # noqa: BLE001 — the hook must go out regardless
        link = None
    if not link:
        return body
    return f"{body}\n\n{IMESSAGE_INVITE.format(link=link)}"


def send_sms(phone: str, body: str, user_id: int = None, message_type: str = "freeform",
             reply_to_sid: str = None):
    """Send an SMS, splitting longer messages into sequential texts with a delay.

    Body is normalized to GSM-7 here (before split + dispatch) so the carrier
    encodes our outbound as 1-segment GSM-7 (160 chars/seg) instead of the
    UCS-2 fallback (67 chars/seg) that gets triggered by a single em-dash or
    smart quote. The transform is the LAST thing we do before split so any
    upstream finalization is captured before dispatch. Logging-mode acks and
    templated stats lines benefit too — any
    `✓` glyph would force UCS-2 if it slipped through.

    See sms_encoding.py for the character map and why we don't rely solely
    on Twilio's server-side Smart Encoding toggle.
    """
    # Opt-out backstop: an opted-out user gets NO sends (proactive or reactive). The
    # opt-out goodbye is sent BEFORE the flag flips, so it isn't blocked here. Flag-gated,
    # so this is a zero-cost no-op until STOP_OPTOUT_ENABLED. (An opted-out user's own
    # inbound resumes them before the coach ever replies; this catches stray proactive sends.)
    if user_id and config.STOP_OPTOUT_ENABLED:
        from optout import is_opted_out
        if is_opted_out(user_id):
            logger.info("SEND_SUPPRESSED_OPTED_OUT user=%s type=%s", user_id, message_type)
            return None

    # Buffer-race backstop: if this is byte-for-byte the reply we just sent this
    # user, the second (duplicate) turn from a raced flush is producing it — drop
    # it before it hits either channel. Runs above the router so it covers both.
    if _is_duplicate_send(user_id, body):
        logger.info("SEND_SUPPRESSED_DUPLICATE user=%s type=%s", user_id, message_type)
        return None

    # Photon migration 4A: route first. iMessage → sidecar, one call, full body.
    # On a hard failure for a user who is NOT on their line yet: write the `failed`
    # row FIRST (the keystone reads it), trip the breaker, then fall through to
    # Twilio so the same message still lands. For a user who IS on their line
    # (opted in): retry, then hold on that line — never a different number.
    if _resolve_channel(user_id) == "imessage":
        # Each `---` part is its own blue bubble, threaded on the first only. The first
        # bubble is the pivot: if IT fails the pipe is down and the WHOLE message takes
        # the failure path; a later bubble failing logs its row and stops — the earlier
        # bubbles landed.
        bubbles = [_imessage_body(b) for b in split_bubbles(body)]
        hold_ok = _hold_eligible(user_id)
        if hold_ok:
            from held_outbound import hold, pending_count, drain_user
            # Order + economy: while Photon is known-down, or earlier messages to
            # this user are still parked, this one queues straight behind them
            # (one probe per drain tick, and they read in the order the coach
            # said them). If the backlog drains right now, send live.
            if outage_active():
                hold(user_id, phone, body, bubbles, message_type, reply_to_sid,
                     reason="outage_active")
                return None
            if pending_count(user_id) and not drain_user(user_id):
                hold(user_id, phone, body, bubbles, message_type, reply_to_sid,
                     reason="queued_behind")
                return None

        first_sid, e, is_timeout = _send_bubbles(phone, bubbles, reply_to_sid, user_id, message_type)
        if e is None:
            return first_sid
        # First bubble failed → clear the dots.
        _typing_stop(user_id)
        # A read timeout on the first bubble: the request was sent, so Photon may
        # have delivered it. Falling over to SMS here is what double-sends the
        # message, and a slow ack is not a dead pipe — so do NOT trip the breaker
        # and do NOT fall through to Twilio. (A connect error / non-2xx is a hard
        # failure and takes the failure path below.)
        if is_timeout:
            logger.warning("IMESSAGE_SEND_TIMEOUT user_id=%s message_type=%s err=%s — sidecar may "
                           "have delivered; NOT failing over to SMS, NOT tripping breaker",
                           user_id, message_type, e)
            return first_sid
        # A stale threaded-reply target is not an outage: the same text goes as a
        # plain bubble. Only then judge the result.
        if reply_to_sid and _is_reply_target_missing(e):
            logger.info("IMESSAGE_REPLY_TARGET_MISSING user_id=%s target=%s — resending unthreaded",
                        user_id, reply_to_sid)
            reply_to_sid = None
            first_sid, e, is_timeout = _send_bubbles(phone, bubbles, None, user_id, message_type)
            if e is None or is_timeout:
                return first_sid
        if _is_consent_gate(e):
            # They haven't texted their line yet → Twilio IS their number. Unchanged.
            _log_message(user_id, bubbles[0], message_type, channel="imessage",
                         provider_sid=None, delivery_status="failed")
            logger.info("IMESSAGE_NOT_OPTED_IN user_id=%s message_type=%s — they haven't texted "
                        "their line yet; falling over to SMS", user_id, message_type)
            if message_type in ("onboarding", "onboarding_bigask"):
                body = _with_imessage_invite(user_id, body)
            _mark_failed_over(user_id)
        elif hold_ok:
            # One number. Retry briefly on the same line, then park it there.
            for delay in config.IMESSAGE_HOLD_RETRY_BACKOFF_S:
                time.sleep(delay)
                first_sid, e, is_timeout = _send_bubbles(phone, bubbles, reply_to_sid, user_id, message_type)
                if e is None or is_timeout:
                    logger.info("IMESSAGE_SEND_RECOVERED user_id=%s message_type=%s after retry",
                                user_id, message_type)
                    return first_sid
            transient = _is_transient_sidecar_error(e)
            logger.log(logging.WARNING if transient else logging.ERROR,
                       "IMESSAGE_HELD user_id=%s message_type=%s transient=%s err=%s — holding on "
                       "their line, NOT falling over to SMS, NOT tripping breaker",
                       user_id, message_type, transient, e)
            note_outage()
            hold(user_id, phone, body, bubbles, message_type, reply_to_sid,
                 reason="transient" if transient else "hard", error=str(e))
            return None
        else:
            _log_message(user_id, bubbles[0], message_type, channel="imessage",
                         provider_sid=None, delivery_status="failed")
            logger.error("IMESSAGE_SEND_FAILED user_id=%s message_type=%s err=%s — failing over to SMS",
                         user_id, message_type, e)
            _mark_failed_over(user_id)

    # Last transform before dispatch — normalize once on the full body so the
    # warning log (next 6 lines) reports per-logical-message, not per-segment.
    body = normalize_for_sms(body)

    # Telemetry: residual non-GSM (e.g. an emoji slipped through) forces UCS-2
    # and roughly halves capacity. Oversized GSM-7 bodies risk delivery limits.
    # Logged, never blocked — coaching content keeps flowing.
    residual = residual_non_gsm(body)
    enc, segs = estimate_segments(body)
    if residual:
        logger.warning(
            "SMS_UCS2 user_id=%s message_type=%s segments=%d chars=%d residual=%s",
            user_id, message_type, segs, len(body), residual,
        )
    elif segs > SMS_SEGMENT_WARN_THRESHOLD:
        logger.warning(
            "SMS_LARGE_GSM user_id=%s message_type=%s segments=%d chars=%d",
            user_id, message_type, segs, len(body),
        )

    parts = split_bubbles(body)

    last_sid = None
    for i, part in enumerate(parts):
        if i > 0:
            time.sleep(bubble_delay(parts[i - 1]))
        try:
            last_sid = _send_single(phone, part)
        except Exception:
            # The row is the keystone's evidence that this didn't land. Write it,
            # then re-raise — callers' existing error handling is unchanged.
            if user_id:
                _log_message(user_id, part, message_type,
                             channel="sms", provider_sid=None, delivery_status="failed")
            raise
        if user_id:
            _log_message(user_id, part, message_type,
                         channel="sms", provider_sid=last_sid, delivery_status="sent")

    return last_sid


def log_incoming(user_id: int, body: str, message_type: str = "freeform",
                 has_image: bool = False, channel: str = "sms", provider_sid: str | None = None):
    """Log an incoming SMS to the database. `has_image` appends IMAGE_MARKER so the
    stored row (the only thing the conversation window ever sees) records that media
    was attached — a captionless MMS logs the marker alone, never an empty body."""
    if has_image:
        body = f"{body} {IMAGE_MARKER}" if body else IMAGE_MARKER
    session = get_session()
    try:
        msg = Message(
            user_id=user_id,
            direction="in",
            body=body,
            message_type=message_type,
            channel=channel,
            provider_sid=provider_sid,
            delivery_status="delivered",  # an inbound we hold is, by definition, delivered to us
        )
        session.add(msg)
        session.commit()
    finally:
        session.close()
    # Layered wake model: an inbound is "they're on their phone". Fail-open, own session.
    try:
        from wake_model import touch_last_active
        touch_last_active(user_id)
    except Exception:  # noqa: BLE001
        pass


def get_twiml_response(body: str = None):
    """Build a TwiML response. If body is None, return empty (we'll respond async)."""
    resp = MessagingResponse()
    if body:
        resp.message(body)
    return str(resp)

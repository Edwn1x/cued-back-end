"""
Per-call Anthropic cost telemetry. One row in token_usage per messages.create()
call. Persisted because Railway logs are ephemeral and can't be aggregated
historically — the Finances dashboard needs a durable source.

Design contracts:
  - record_usage() is best-effort. It MUST NEVER raise into the response
    path. A telemetry failure cannot break a user's coaching reply.
  - cost_usd is computed at insert time and stored. Raw token buckets are
    also stored so we can recompute, but historical totals stay correct
    even if MODEL_PRICING changes later.
  - Inline call by default. If post-deploy latency metrics show this in the
    response path, move the call into a daemon thread — the API is the same.

See plans/cued-memory-architecture-joyful-ullman.md — Phase C1.5.
"""
import logging

import config

logger = logging.getLogger(__name__)


def _model_key(model_str: str) -> str | None:
    """Map Anthropic model id strings -> the keys in config.MODEL_PRICING.
    Returns None for unknown models (caller treats as a no-op)."""
    if not model_str:
        return None
    s = model_str.lower()
    if "sonnet" in s:
        return "sonnet"
    if "haiku" in s:
        return "haiku"
    return None


def compute_cost(model_key: str, usage) -> float:
    """
    Compute USD cost for one Anthropic messages.create response.usage.

    Formula (matches Anthropic SDK semantics — the three input buckets are
    mutually exclusive: input_tokens is fresh uncached input):
      cost = ( input_tokens                * in_rate
             + cache_creation_input_tokens * in_rate * 1.25
             + cache_read_input_tokens     * in_rate * 0.10
             + output_tokens               * out_rate ) / 1_000_000

    Uses getattr defaults so older SDK responses missing the cache fields
    just contribute 0 (correct — caching wasn't on).
    """
    p = config.MODEL_PRICING[model_key]
    inp = getattr(usage, "input_tokens", 0) or 0
    cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (
        inp * p["input"]
        + cc * p["input"] * config.CACHE_WRITE_MULTIPLIER
        + cr * p["input"] * config.CACHE_READ_MULTIPLIER
        + out * p["output"]
    ) / 1_000_000


def record_usage(user_id, site: str, model_str: str, usage) -> None:
    """
    Persist one Anthropic call to token_usage. Best-effort: any exception
    is swallowed and logged so a telemetry failure can't break the user's
    coaching response.

    Args:
      user_id: int or None (None for system calls with no user in scope).
      site: stable string identifying the call site (e.g.
            "coach.get_coach_response"). Becomes the per-site breakdown key
            in the Finances dashboard, so use a consistent vocabulary.
      model_str: the model id passed to messages.create (e.g.
                 "claude-sonnet-4-6"). We classify into "sonnet"/"haiku".
      usage: response.usage from the SDK. None-safe.
    """
    try:
        mk = _model_key(model_str)
        if mk is None or usage is None:
            return
        from models import get_session, TokenUsage
        session = get_session()
        try:
            row = TokenUsage(
                user_id=user_id,
                site=site,
                model=mk,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cost_usd=compute_cost(mk, usage),
            )
            session.add(row)
            session.commit()
        finally:
            session.close()
    except Exception as e:
        # Never raise into the response path.
        logger.error("record_usage failed (site=%s): %s", site, e)


def log_tokens(user_id, site: str, model_str: str, usage) -> None:
    """
    Live-tail log line for tracking cache hits in real time. Pair with
    record_usage() (which is the durable record) at every call site.
    Cheap structured log — grep prod logs for `TOKENS site=X cache_read=N`
    to see caching working without hitting the DB.
    """
    if usage is None:
        return
    try:
        logger.info(
            "TOKENS user=%s site=%s model=%s input=%d output=%d cache_create=%d cache_read=%d",
            user_id, site, model_str,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    except Exception:
        pass


def track(user_id, site: str, model_str: str, usage) -> None:
    """
    Convenience: emit the TOKENS log AND persist the row in one call.
    Use this at every messages.create() site — one line of instrumentation
    instead of two. Both halves are best-effort.
    """
    log_tokens(user_id, site, model_str, usage)
    record_usage(user_id, site, model_str, usage)

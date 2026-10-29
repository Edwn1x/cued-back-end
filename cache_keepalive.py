"""Prompt-cache keep-alive: keep the warm 1-hour prefixes warm across quiet gaps.

Every Opus site caches a stable prefix (tools + the voice prompt, + routing on the
reply loop) with the 1-hour TTL. When no call reuses a prefix for an hour it expires,
and the next call rewrites it at 2× input price — ~$0.47 for the 58k-token reply
prefix, ~$0.28 for the heartbeat's. Live 2026-10-28: 15 such writes were 66% of the
day's spend, almost all after overnight gaps of 1.5–3h between replies.

A cache READ refreshes the entry's timer. So: each real call registers the exact
prefix it sent (`note`), and a 5-minute sweep (`run_due`) re-sends that prefix with
max_tokens=0 once it has gone REFRESH_MIN without use — a pure prefill that bills
only the cache read (~$0.006–0.012) and returns no content. It never reaches a
user, never runs a tool, never generates a token, so model output can't change.

Bounds: a prefix stops being kept alive IDLE_HOURS after its last REAL use (no
pinging forever when nobody texts); at most MAX_ENTRIES prefixes; a ping that comes
back with no cache read (bytes drifted, entry evicted) drops that prefix instead of
paying a write every 50 minutes. Process-local by design: one web process owns
both the calls and the scheduler; a deploy starts empty and the first real call
re-registers. Every ping is tracked as site "cache.keepalive" (user None).
"""
import copy
import hashlib
import json
import logging
import threading
import time

import config

logger = logging.getLogger(__name__)

SITE = "cache.keepalive"
# Read during prefill, never answered (max_tokens=0 returns content=[]).
_PLACEHOLDER = "keepalive"
# Stands in for the uncached per-call tail. A breakpoint block followed by ANOTHER
# system block caches one more token (the separator) than the same block ending the
# system: live 2026-10-28 the reminder prefix was 28,675 tokens, a ping of [voice]
# alone 28,674 — a miss that paid a write. So the ping keeps the real call's shape.
_TAIL = {"type": "text", "text": "."}

_lock = threading.Lock()
_entries: dict[str, dict] = {}


def _cached_prefix(system):
    """The system blocks through the last one carrying a 1-hour cache_control, or None
    when there is nothing a keep-alive can hold (string system, no breakpoint, or a
    5-minute TTL — pinging every 4 minutes would cost more than the writes it saves)."""
    if not isinstance(system, list):
        return None
    last = None
    for i, block in enumerate(system):
        cc = block.get("cache_control") if isinstance(block, dict) else None
        if cc:
            if cc.get("ttl") != "1h":
                return None
            last = i
    return None if last is None else system[: last + 1]


def note(site: str, *, model: str, system, tools=None, thinking=None, output_config=None,
         now: float | None = None) -> None:
    """Record that a real call just STARTED on this prefix. Call right before
    messages.create with the same arguments. Never raises into the caller."""
    if not config.CACHE_KEEPALIVE_ENABLED:
        return
    try:
        prefix = _cached_prefix(system)
        if prefix is None:
            return
        tail = len(system) > len(prefix)
        now = time.time() if now is None else now
        key = hashlib.sha1(json.dumps(
            [model, tools or None, prefix, tail, thinking, output_config],
            sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with _lock:
            entry = _entries.get(key)
            if entry is None:
                if len(_entries) >= config.CACHE_KEEPALIVE_MAX_ENTRIES:
                    oldest = min(_entries, key=lambda k: _entries[k]["last_real"])
                    _entries.pop(oldest)
                # Snapshot: the replay must be the exact bytes the real call sent, in
                # its original order (the key above is order-normalized only for lookup).
                entry = _entries[key] = {
                    "site": site, "model": model,
                    "system": copy.deepcopy(prefix) + ([dict(_TAIL)] if tail else []),
                    "tools": copy.deepcopy(tools) if tools else None,
                    "thinking": copy.deepcopy(thinking),
                    "output_config": copy.deepcopy(output_config),
                    "pings": 0,
                }
            entry["site"] = site
            entry["last_real"] = now
            entry["last_touch"] = now
    except Exception as e:  # noqa: BLE001 — telemetry-grade; never break a reply
        logger.warning("CACHE_KEEPALIVE_NOTE_FAILED site=%s err=%s", site, e)


def _ping(entry: dict):
    from llm_client import make_client
    kwargs = dict(model=entry["model"], max_tokens=0, system=entry["system"],
                  messages=[{"role": "user", "content": _PLACEHOLDER}])
    if entry["tools"]:
        kwargs["tools"] = entry["tools"]
    if entry["thinking"] is not None:
        kwargs["thinking"] = entry["thinking"]
    if entry["output_config"] is not None:
        kwargs["output_config"] = entry["output_config"]
    return make_client().messages.create(**kwargs)


def run_due(now: float | None = None) -> dict:
    """Scheduler sweep: ping every prefix that has gone REFRESH_MIN untouched, drop
    the ones idle past IDLE_HOURS. Returns counts (for tests and the log line)."""
    out = {"pinged": 0, "dropped_idle": 0, "dropped_miss": 0, "failed": 0}
    if not config.CACHE_KEEPALIVE_ENABLED:
        return out
    now = time.time() if now is None else now
    refresh_s = config.CACHE_KEEPALIVE_REFRESH_MIN * 60
    idle_s = config.CACHE_KEEPALIVE_IDLE_HOURS * 3600
    with _lock:
        for key in [k for k, e in _entries.items() if now - e["last_real"] > idle_s]:
            e = _entries.pop(key)
            out["dropped_idle"] += 1
            logger.info("CACHE_KEEPALIVE_IDLE site=%s pings=%d", e["site"], e["pings"])
        due = [(k, e) for k, e in _entries.items() if now - e["last_touch"] >= refresh_s]
    for key, entry in due:
        try:
            resp = _ping(entry)
        except Exception as e:  # noqa: BLE001 — a failed ping just lets the entry lapse
            out["failed"] += 1
            logger.warning("CACHE_KEEPALIVE_FAILED site=%s err=%s", entry["site"], e)
            continue
        try:
            from cost_tracking import track
            track(None, SITE, entry["model"], resp)
        except Exception as e:  # noqa: BLE001
            logger.warning("CACHE_KEEPALIVE_COST_TRACK_FAILED err=%s", e)
        usage = getattr(resp, "usage", None)
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        wrote = getattr(usage, "cache_creation_input_tokens", 0) or 0
        with _lock:
            if read == 0:
                # Nothing warm to hold (bytes drifted or the entry was evicted): the
                # ping just paid a write. Stop — the next real call re-registers.
                _entries.pop(key, None)
                out["dropped_miss"] += 1
                logger.warning("CACHE_KEEPALIVE_MISS site=%s wrote=%d — prefix dropped",
                               entry["site"], wrote)
                continue
            if key in _entries:
                # Lifetime runs from the request START, so stamp the pre-ping time.
                _entries[key]["last_touch"] = now
                _entries[key]["pings"] += 1
        out["pinged"] += 1
        logger.info("CACHE_KEEPALIVE_PING site=%s read=%d idle_min=%d",
                    entry["site"], read, int((now - entry["last_real"]) / 60))
    return out


def _reset_for_tests() -> None:
    with _lock:
        _entries.clear()

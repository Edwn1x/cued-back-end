"""Who is actually calling — the rate-limiter keys behind Railway's edge.

Live finding (2026-10-16, after the launch-night probe): ten identical bad POSTs to
/signup from ONE laptop came back 400×5, 429×2, 400×3. Each request reached the app
from a different Railway edge hop (79.127.217.x, 152.233.76.x …) and the 429s landed
exactly on the hop that had already passed three — so the per-IP limiter was keying
on the HOP, not the client. Every public limit was ~6× looser than written and the
budget was pooled across all real users behind the same hop.

What Railway actually sends (staff answers on station.railway.com, 2024-08 → 2025):
  * `X-Real-IP` is overwritten at the edge with the client IP (clients can't set it
    since 2024-08-23) — their recommended header.
  * `X-Forwarded-For` keeps whatever the client sent and APPENDS the client IP, so
    only the RIGHTMOST entry is trustworthy. (ProxyFix x_for=1 reads that too.)
  * Known edge/CDN-path bug: on some hops both carry the hop's own IP. Nothing in
    the app can recover the client there — which is why the sign-up routes also
    key on the PHONE (request_ip.phone_key) and carry a shared ceiling.

So: `client_ip()` prefers X-Real-IP, then the rightmost valid X-Forwarded-For entry,
then the TCP peer — and never a client-controlled left entry. `forwarding_snapshot()`
is logged on every 429 so prod shows which header carried what.
"""
from __future__ import annotations

import ipaddress
import re

from flask import request

FORWARDING_HEADERS = ("X-Real-IP", "X-Forwarded-For", "X-Envoy-External-Address")


def _ip_or_none(raw) -> str | None:
    """A normalized IP literal, or None for anything else (blank, garbage, a name)."""
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(str(raw).strip()))
    except ValueError:
        return None


def client_ip() -> str:
    """Best available client address for this request (see module docstring)."""
    real = _ip_or_none(request.headers.get("X-Real-IP"))
    if real:
        return real
    xff = request.headers.get("X-Forwarded-For", "") or ""
    for part in reversed(xff.split(",")):          # rightmost = the one the edge appended
        ip = _ip_or_none(part)
        if ip:
            return ip
    return _ip_or_none(request.remote_addr) or "unknown"


def phone_key() -> str:
    """Limiter key for the sign-up routes: the phone in the body, digits only, so a
    script can't text one victim over and over by rotating IPs. Reads JSON or form.
    No usable phone → falls back to the client IP so the limit still applies."""
    phone = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        phone = body.get("phone") if isinstance(body, dict) else None
    if phone is None:
        phone = request.form.get("phone")
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) >= 7:
        return "phone:" + digits[-10:]            # national part — +1 / 1 / bare all collide
    return "ip:" + client_ip()


def forwarding_snapshot() -> dict:
    """Raw forwarding headers + TCP peer, for the 429 log line."""
    snap = {h: request.headers.get(h) for h in FORWARDING_HEADERS}
    snap["peer"] = request.remote_addr
    snap["key"] = client_ip()
    return snap

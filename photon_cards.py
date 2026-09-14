"""
Flask → sidecar client for iMessage mini-app cards (workout logger).

Phase 0: the founder's install-flow experiment goes through here (via
/internal/card-test) so nobody needs a shell inside Railway's private network.
Phase 3 reuses the same two calls from the coach tool.

Both raise on any non-2xx / ok=false so the caller can fail over like any send.
"""

from __future__ import annotations

import logging

import requests

import config

logger = logging.getLogger("cued.cards")


class CardError(RuntimeError):
    """The sidecar (or Photon behind it) refused the card."""


def _post(path: str, payload: dict) -> dict:
    if not config.SIDECAR_URL:
        raise CardError("no sidecar configured")
    resp = requests.post(config.SIDECAR_URL.rstrip("/") + path, json=payload,
                         headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
                         timeout=config.SIDECAR_TIMEOUT_S)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 300 or not isinstance(data, dict) or not data.get("ok"):
        raise CardError(f"sidecar {path} {resp.status_code}: {(data.get('error') if isinstance(data, dict) else None) or resp.text[:200]}")
    return data


def send_card(phone: str, url: str, live: bool = True, layout: dict | None = None) -> dict:
    """→ {"provider_message_id": str|None, "card_session": dict|None}. `layout`
    (caption/subcaption/trailingCaption/trailingSubcaption/summary) is the static
    preview in the bubble when `live` is False; tapping it opens `url` in the
    Spectrum extension's sheet (the overlay)."""
    payload = {"phone": phone, "url": url, "live": live}
    if layout:
        payload["layout"] = layout
    data = _post("/send-card", payload)
    logger.info("CARD_SENT phone_last4=%s id=%s live=%s url=%s", phone[-4:], data.get("provider_message_id"), live, url)
    return {"provider_message_id": data.get("provider_message_id"), "card_session": data.get("card_session")}


def update_card(phone: str, card_session: dict, url: str, live: bool | None = None, layout: dict | None = None) -> None:
    payload = {"phone": phone, "card_session": card_session, "url": url}
    if live is not None:
        payload["live"] = live
    if layout:
        payload["layout"] = layout
    _post("/update-card", payload)
    logger.info("CARD_UPDATED phone_last4=%s id=%s url=%s", phone[-4:], (card_session or {}).get("id"), url)

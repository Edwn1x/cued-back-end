"""
Photon / Spectrum Cloud — user provisioning (migration handoff v2, Phase 4C).

On Free/Pro shared-pool plans a phone number must exist in the project's Users
list before any outbound to it succeeds ("Target not allowed for this project").
Flask registers the user here at signup, BEFORE the first outbound, so the
onboarding hook can go blue. Failure — including the Free-tier user cap — is
logged and the user stays on SMS; onboarding never blocks on Photon.

Endpoint pinned to https://spectrum.photon.codes/openapi/json (fetched 2026-09-10,
not guessed):
    POST /projects/{projectId}/users/
    Authorization: Basic base64(projectId:projectSecret)
    body {type: "shared", phoneNumber (E.164), firstName?, lastName?, email?}
    200 → {succeed: true, data: {id, phoneNumber, assignedPhoneNumber, type, ...}}
Re-creating an existing active phoneNumber returns the same user (idempotent).
`type: "dedicated"` (Business plan) also needs assignedPhoneNumber — not wired;
add when the Business-tier trigger fires.
"""

import base64
import logging

import requests

import config
from models import get_session, User

logger = logging.getLogger("cued.photon")


class PhotonError(Exception):
    """The users API rejected the call or answered with an unusable body."""


def _auth_header() -> dict:
    token = base64.b64encode(
        f"{config.SPECTRUM_PROJECT_ID}:{config.SPECTRUM_PROJECT_SECRET}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def add_user(phone: str, name: str | None = None) -> str:
    """Create (or fetch) the shared Photon user for `phone`; return its id.
    Raises PhotonError on any non-2xx or a 2xx without `succeed`/`data.id`."""
    url = f"{config.SPECTRUM_API_URL.rstrip('/')}/projects/{config.SPECTRUM_PROJECT_ID}/users/"
    body = {"type": "shared", "phoneNumber": phone}
    if name and name.strip():
        first, *rest = name.strip().split(" ", 1)
        body["firstName"] = first
        if rest and rest[0].strip():
            body["lastName"] = rest[0].strip()

    resp = requests.post(url, json=body,
                         headers={**_auth_header(), "Content-Type": "application/json"},
                         timeout=config.PHOTON_TIMEOUT_S)
    if resp.status_code >= 300:
        raise PhotonError(f"photon users API {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise PhotonError(f"photon users API non-JSON 2xx: {resp.text[:200]}") from e
    uid = (data.get("data") or {}).get("id") if isinstance(data, dict) else None
    if not data.get("succeed") or not uid:
        raise PhotonError(f"photon users API unusable body: {resp.text[:200]}")
    return uid


def provision_user(user_id: int) -> bool:
    """Register `user_id` with Photon and flip preferred_channel to imessage.
    Returns True when the user is (or already was) provisioned, False otherwise.
    Never raises — a Photon problem must not block onboarding over SMS."""
    if not config.PHOTON_PROVISIONING_ENABLED:
        return False
    if not (config.SPECTRUM_PROJECT_ID and config.SPECTRUM_PROJECT_SECRET):
        logger.warning("PHOTON_PROVISION_SKIPPED user=%s reason=missing_project_creds", user_id)
        return False

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return False
        if user.photon_user_id:
            return True  # idempotent: already on the allowlist
        try:
            photon_id = add_user(user.phone, user.name)
        except Exception as e:  # noqa: BLE001 — includes the Free-tier cap (4xx)
            logger.warning("PHOTON_PROVISION_FAILED user=%s err=%s — staying on sms", user_id, e)
            return False
        user.photon_user_id = photon_id
        user.preferred_channel = "imessage"
        session.commit()
        logger.info("PHOTON_PROVISIONED user=%s photon_user_id=%s", user_id, photon_id)
        return True
    finally:
        session.close()

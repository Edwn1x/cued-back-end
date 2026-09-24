"""OAuth connect flow routes (Blueprint `integrations_bp`, registered in app.py).

    GET /c/<provider>?t=<connect_token>
        The link the coach texts. Verifies the single-use token, then 302s to the
        provider's authorize screen with `state` = the token.

    GET /oauth/<provider>/callback?code=…&state=…
        Provider redirects back here. Validates state (+ single-use nonce),
        exchanges the code, stores encrypted tokens, and texts one confirmation
        through the normal outbound path. On any failure the row goes to `error`
        with the reason in meta.last_error and NO text is sent (spec §0.2).

The Strava webhook route is added to this same blueprint in Part 2.
"""
from __future__ import annotations

import logging

from flask import Blueprint, request, redirect, Response

import config
from models import get_session, User
from integrations import base
from integrations.tokens import verify_connect_token

logger = logging.getLogger("cued.integrations.routes")

integrations_bp = Blueprint("integrations", __name__)


def _page(title: str, body: str, status: int = 200) -> Response:
    html = (
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>body{font:16px -apple-system,system-ui,sans-serif;margin:14vh auto;max-width:20rem;"
        "text-align:center;color:#111;padding:0 1.5rem}h1{font-size:1.1rem;font-weight:600}"
        "p{color:#666}@media(prefers-color-scheme:dark){body{background:#000;color:#eee}p{color:#999}}</style>"
        f"<h1>{title}</h1><p>{body}</p>"
    )
    return Response(html, status=status, mimetype="text/html", headers={"Cache-Control": "no-store"})


def _redirect_uri(provider: str) -> str:
    return f"{config.INTEGRATIONS_BASE_URL.rstrip('/')}/oauth/{provider}/callback"


@integrations_bp.route("/c/<provider>", methods=["GET"])
def connect_start(provider: str):
    prov = base.get_provider(provider)
    if prov is None or not prov.enabled():
        return _page("not available", "this connection isn't turned on yet.", 404)

    parsed = verify_connect_token(request.args.get("t"))
    if not parsed or parsed[1] != provider:
        return _page("link expired", "ask the coach to send a fresh link.", 400)
    user_id, _, nonce = parsed

    # single-use: the row must still be holding exactly this nonce
    if base.pending_nonce(user_id, provider) != nonce:
        return _page("link already used", "ask the coach to send a fresh link.", 400)

    try:
        url = prov.authorize_url(state=request.args["t"], redirect_uri=_redirect_uri(provider))
    except Exception as e:
        logger.exception("CONNECT_START_FAILED provider=%s user=%s", provider, user_id)
        base.mark_error(user_id, provider, f"authorize_url: {e}")
        return _page("something went wrong", "try again in a bit.", 500)
    return redirect(url, code=302)


@integrations_bp.route("/oauth/<provider>/callback", methods=["GET"])
def oauth_callback(provider: str):
    prov = base.get_provider(provider)
    if prov is None:
        return _page("not available", "unknown provider.", 404)

    # user denied / provider error → leave the row as-is, no text, neutral page
    if request.args.get("error"):
        return _page("no worries", "you can close this and head back to Messages.")

    parsed = verify_connect_token(request.args.get("state"))
    if not parsed or parsed[1] != provider:
        return _page("link expired", "ask the coach to send a fresh link.", 400)
    user_id, _, nonce = parsed

    if base.pending_nonce(user_id, provider) != nonce:
        return _page("link already used", "ask the coach to send a fresh link.", 400)

    code = request.args.get("code")
    if not code:
        base.mark_error(user_id, provider, "no code in callback")
        return _page("something went wrong", "try again in a bit.", 400)

    try:
        bundle = prov.exchange_code(code, redirect_uri=_redirect_uri(provider))
    except Exception as e:
        logger.exception("OAUTH_EXCHANGE_FAILED provider=%s user=%s", provider, user_id)
        base.mark_error(user_id, provider, f"exchange: {e}")
        return _page("couldn't connect", "try again in a bit.", 502)

    base.complete_connection(user_id, provider, bundle)

    # Immediate first pull so the user isn't blind until the next scheduled sync — they
    # often ask "what's on it?" seconds after connecting. Best-effort; a sync failure
    # must not fail the connect (the scheduler will catch up).
    try:
        prov.sync_now(user_id)
    except Exception:
        logger.exception("OAUTH_SYNC_NOW_FAILED provider=%s user=%s", provider, user_id)

    # one confirmation text through the normal outbound path — success only
    try:
        session = get_session()
        try:
            user = session.get(User, user_id)
            phone = user.phone if user else None
            integ = base.get_integration(session, user_id, provider)
            msg = prov.connected_message(integ)
        finally:
            session.close()
        if phone:
            from sms import send_sms
            send_sms(phone, msg, user_id=user_id, message_type="integration_connected")
    except Exception:
        logger.exception("OAUTH_CONFIRM_SEND_FAILED provider=%s user=%s", provider, user_id)
        # the connection still succeeded; just the confirmation text failed

    return _page("connected", "you're all set — head back to Messages.")


# ─── Google Health API webhook (Part 2a) ─────────────────────────────────────
#
#   POST /oauth/google_health/webhook
#     Registered once per Cloud project (scripts/register_google_health_subscriber.py)
#     with endpointAuthorization.secret = GOOGLE_HEALTH_WEBHOOK_SECRET; Google sends
#     that value verbatim in the Authorization header. Two request kinds:
#       verification  {"type": "verification"} — must answer 200/201 WITH the secret and
#                     401/403 WITHOUT it (Google sends both to prove we check).
#       notification  {"data": {healthUserId, dataType, operation, intervals…}} or a
#                     JSON array of those when batched — answer 204 immediately (anything
#                     else is retried with backoff for 7 days) and sync off-thread.
#     Lives outside /admin so the basic-auth gate never blocks Google.

@integrations_bp.route("/oauth/google_health/webhook", methods=["POST"])
def google_health_webhook():
    import secrets as _secrets
    secret = config.GOOGLE_HEALTH_WEBHOOK_SECRET
    given = request.headers.get("Authorization") or ""
    if not (secret and given and _secrets.compare_digest(given, secret)):
        return Response(status=401)
    payload = request.get_json(force=True, silent=True)
    if isinstance(payload, dict) and payload.get("type") == "verification":
        return Response(status=201)
    if not config.GOOGLE_HEALTH_ENABLED:
        return Response(status=204)   # acknowledge so Google doesn't back off + retry for days
    try:
        from integrations import google_health_sync
        google_health_sync.handle_notifications(payload)
    except Exception:
        logger.exception("GOOGLE_HEALTH_NOTIFY_FAILED")
    return Response(status=204)


__all__ = ["integrations_bp"]

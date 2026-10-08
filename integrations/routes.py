"""OAuth connect flow routes (Blueprint `integrations_bp`, registered in app.py).

    GET /c/<provider>/<code>
        The link the coach texts (short, on CONNECT_LINK_BASE_URL). The code is the
        row's single-use nonce; the signed token is rebuilt from the row and passed
        on as `state` from a cued-branded handoff page.

    GET /c/<provider>?t=<connect_token>
        The original long link, still honored for links already sent. Verifies the
        single-use token, then 302s to the provider's authorize screen.

    GET /oauth/<provider>/callback?code=…&state=…
        Provider redirects back here. Validates state (+ single-use nonce),
        exchanges the code, stores encrypted tokens, and texts one confirmation
        through the normal outbound path. On any failure the row goes to `error`
        with the reason in meta.last_error and NO text is sent (spec §0.2).

The Strava webhook route is added to this same blueprint in Part 2.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading

from flask import Blueprint, request, redirect, Response

import config
from models import get_session, User
from integrations import base
from integrations.tokens import connect_token, verify_connect_token

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


def _connected_page() -> Response:
    return _page("you're connected", "head back to Messages — the coach has it.")


def _already_used(user_id: int | None, provider: str) -> Response:
    """A dead link for a user who IS connected is a re-tap of the link that worked (or a
    stale one) — say so instead of 'ask for a fresh link' (live 2026-10-06, user 48)."""
    if user_id is not None and base.is_connected(user_id, provider):
        return _connected_page()
    return _page("link already used", "ask the coach to send a fresh link.", 400)


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
        return _already_used(user_id, provider)

    try:
        url = prov.authorize_url(state=request.args["t"], redirect_uri=_redirect_uri(provider))
    except Exception as e:
        logger.exception("CONNECT_START_FAILED provider=%s user=%s", provider, user_id)
        base.mark_error(user_id, provider, f"authorize_url: {e}")
        return _page("something went wrong", "try again in a bit.", 500)
    return redirect(url, code=302)


_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
_OG_IMAGE = "https://cued.fit/images/og-image-cued.png"


def _handoff_page(label: str, authorize_url: str) -> Response:
    """The short link's landing: cued-branded link-preview tags for the iMessage bubble,
    then straight on to the provider's sign-in (meta refresh + JS, a tap-through fallback)."""
    title = html.escape(f"connect your {label} · cued")
    desc = html.escape(f"one tap and i can see your {label}.")
    href = html.escape(authorize_url, quote=True)
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title>"
        f"<meta property='og:title' content='{title}'>"
        f"<meta property='og:description' content='{desc}'>"
        "<meta property='og:site_name' content='cued'>"
        f"<meta property='og:image' content='{_OG_IMAGE}'>"
        f"<meta http-equiv=refresh content='0;url={href}'>"
        "<style>body{font:16px -apple-system,system-ui,sans-serif;margin:14vh auto;max-width:20rem;"
        "text-align:center;color:#111;padding:0 1.5rem}h1{font-size:1.1rem;font-weight:600}"
        "a{color:#0a84ff}@media(prefers-color-scheme:dark){body{background:#000;color:#eee}}</style>"
        f"<script>location.replace({json.dumps(authorize_url)})</script></head><body>"
        f"<h1>connecting your {html.escape(label)}…</h1>"
        f"<p><a href='{href}'>tap here if nothing happens</a></p></body></html>"
    )
    return Response(page, mimetype="text/html", headers={"Cache-Control": "no-store"})


@integrations_bp.route("/c/<provider>/<code>", methods=["GET"])
def connect_short(provider: str, code: str):
    """The short link: the code is the row's live single-use nonce. Rebuild the signed
    token from the row and hand off to the provider with it as `state`, so the callback
    (and its single-use check) is exactly the long-link path's."""
    prov = base.get_provider(provider)
    if prov is None or not prov.enabled():
        return _page("not available", "this connection isn't turned on yet.", 404)
    if not _CODE_RE.match(code or ""):
        return _page("link expired", "ask the coach to send a fresh link.", 400)

    hit = base.pending_by_code(provider, code)
    if hit is None:
        return _already_used(base.connected_by_code(provider, code), provider)
    user_id, exp = hit
    token, _ = connect_token(user_id, provider, nonce=code, exp=exp)
    if verify_connect_token(token) is None:      # past exp
        return _page("link expired", "ask the coach to send a fresh link.", 400)

    try:
        url = prov.authorize_url(state=token, redirect_uri=_redirect_uri(provider))
    except Exception as e:
        logger.exception("CONNECT_START_FAILED provider=%s user=%s", provider, user_id)
        base.mark_error(user_id, provider, f"authorize_url: {e}")
        return _page("something went wrong", "try again in a bit.", 500)
    return _handoff_page(getattr(prov, "label", None) or provider, url)


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
        return _already_used(user_id, provider)

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
    # must not fail the connect (the scheduler will catch up). In the BACKGROUND: inline it
    # held the page open for 3 minutes (135 events, user 48) and the user's retries hit
    # "link already used" while the first callback was still running.
    def _first_pull():
        try:
            prov.sync_now(user_id)
            logger.info("OAUTH_SYNC_NOW_DONE provider=%s user=%s", provider, user_id)
        except Exception:
            logger.exception("OAUTH_SYNC_NOW_FAILED provider=%s user=%s", provider, user_id)

    # one confirmation text through the normal outbound path — success only, BEFORE the pull
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

    if config.OAUTH_SYNC_IN_BACKGROUND:
        threading.Thread(target=_first_pull, name=f"oauth-sync-{provider}-{user_id}", daemon=True).start()
    else:
        _first_pull()

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
    import hmac as _hmac
    secret = (config.GOOGLE_HEALTH_WEBHOOK_SECRET or "").strip()
    given = (request.headers.get("Authorization") or "").strip()
    # bytes, not str: secrets/hmac.compare_digest on str REQUIRES ASCII and raises
    # TypeError (→ 500) otherwise — live 2026-09-24, the first configured secret had a
    # non-ASCII character. Header values are ASCII in practice; stay total regardless.
    if not (secret and given and _hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))):
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

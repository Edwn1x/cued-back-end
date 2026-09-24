#!/usr/bin/env python3
"""One-off: register Cued's webhook endpoint with the Google Health API.

Subscribers are PROJECT-level (not per user) and need a Cloud-project credential with
the health.subscribers.create permission — i.e. the project owner, not a user's OAuth
token. Run it once from a machine where gcloud is logged in as the owner of the Cloud
project that holds GOOGLE_OAUTH_CLIENT_ID:

    gcloud auth login
    gcloud auth print-access-token   # (used below)
    python3 scripts/register_google_health_subscriber.py \
        --project-number 1234567890 \
        --endpoint https://web-production-90171c.up.railway.app/oauth/google_health/webhook \
        --secret "Bearer <the value you set as GOOGLE_HEALTH_WEBHOOK_SECRET in Railway>"

The endpoint must ALREADY be deployed with that secret: Google verifies it during the
call (a POST {"type":"verification"} with the secret → expects 201, and one without →
expects 401). AUTOMATIC subscriptionCreatePolicy means every user who grants the
scopes is subscribed without a per-user call.

Data type names in subscriberConfigs are the API's kebab-case ids. If Google rejects a
name with a 400, drop it via --data-types and re-run; the sync polls every 30 min
regardless, so a missing subscription only costs freshness.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

API = "https://health.googleapis.com/v4"
DEFAULT_TYPES = ["steps", "sleep", "weight", "daily-resting-heart-rate",
                 "daily-heart-rate-variability", "active-zone-minutes", "total-calories"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-number", required=True, help="Cloud project NUMBER (not id)")
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--secret", required=True, help="exact Authorization header value Google will send")
    ap.add_argument("--subscriber-id", default="cued-webhook")
    ap.add_argument("--data-types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--token", help="access token; default = `gcloud auth print-access-token`")
    a = ap.parse_args()

    token = a.token or subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    body = {
        "endpointUri": a.endpoint,
        "endpointAuthorization": {"secret": a.secret},
        "subscriberConfigs": [{"dataTypes": [t.strip() for t in a.data_types.split(",") if t.strip()],
                               "subscriptionCreatePolicy": "AUTOMATIC"}],
    }
    url = f"{API}/projects/{a.project_number}/subscribers?subscriberId={a.subscriber_id}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(r.status, r.read().decode())
            return 0
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

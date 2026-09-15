# Waitwell (RSF virtual weight-room line) — what the page does (2026-09-14)

**Entry:** `https://417804.waitwell.us/` and the join form `https://417804.waitwell.us/join/48`
(the FAQ's link). Rules from RecWell's FAQ: the line opens whenever the crowd meter is ≥95%;
join by phone number (international ok) or name; updates by text; 10-minute window after being
summoned; everyone admitted individually; a kiosk at the door takes a name.

**What a server sees.** Both URLs answer **HTTP 403 with a Cloudflare JavaScript challenge**
to any non-browser client — including a mobile Safari User-Agent:

```
HTTP/2 403
cf-mitigated: challenge
server: cloudflare
… "Just a moment" / challenges.cloudflare.com / __cf_chl …
```

No pre-join "estimated wait" or "line open" endpoint is reachable; no join request can be
observed or replayed from a server. The page is an Angular SPA (`data-beasties-container`) whose
API calls only happen after the challenge clears in a real browser.

**Consequence (per the brief's own rule):** `client.join()` raises `QueueUnavailable` by
construction — on the challenge (403 + `cf-mitigated`), on any non-2xx, and on a changed
response shape — and the gym beat falls back to **D1**: the join-now link with the user's
walking time. The client keeps the D2 contract (`join`/`status`/`leave`, `queue_tickets`
table, one open ticket per user) so that when a transport exists — Waitwell's own API, a
RecWell-provided integration (founder's heads-up email), or a headless-browser bridge if we
ever decide that's acceptable — it is a transport swap, not a rebuild.

**Not attempted:** headless-browser challenge solving. It's brittle, arguably against the
site's intent, and the brief classifies it under "can't be reproduced server-side".

**Observed `est_wait`:** none available pre-join → `line_on` is inferred from `pct >= 95`.

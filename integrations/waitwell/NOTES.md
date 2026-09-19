# Waitwell (RSF virtual weight-room line) — what the page does (2026-09-14)

**Entry:** `https://417804.waitwell.us/` and the join form `https://417804.waitwell.us/join/48`
(the FAQ's link). Rules from RecWell's FAQ: the line opens whenever the crowd meter is ≥95%;
join by phone number (international ok) or name; updates by text; 10-minute window after being
summoned; everyone admitted individually; a kiosk at the door takes a name.

**What a server sees (re-probed 2026-09-19 — this corrects the 09-14 note).** There are TWO
hosts. The SPA host `417804.waitwell.us` (the landing page and `/join/48`) answers **HTTP 403
with a Cloudflare JavaScript challenge** to any non-browser client — including a mobile Safari
User-Agent:

```
HTTP/2 403
cf-mitigated: challenge
server: cloudflare
… "Just a moment" / challenges.cloudflare.com / __cf_chl …
```

The page is an Angular SPA (`data-beasties-container`); its bundles are served unchallenged
(`/robots.txt` and the `*.js` chunks return 200) and they name the real backend: the API host is
**`https://api.waitwell.us/api/<siteID>/client/...`** (`.ca` for Canadian sites; the app switches
by TLD; DNS: AWS Global Accelerator / ALB, **not** behind Cloudflare). That host answers plain curl
with JSON and **no challenge** — `client/state`, `client/locationstatus?Queue_id=…`,
`client/servicetypes`, `client/booklink/…` all return 200. What we could NOT do: resolve the site
token the app gets from `client/urlmap?from=417804.waitwell.us` (every variant → "Invalid link"),
so those reads returned `{invalid:true}` ("link incomplete") rather than RSF's settings; sending the
app's own `X-AppToken` header flips the answer to error 45206 (`DB_TOO_MANY_CONNECTIONS` in their
enum), and the host rate-limits (429) after a handful of requests. So a **read** path (live line
status / est. wait without joining) is plausible but unfinished; treat it as a follow-up, and go
gently on their API.

**What gates a join (from the join chunk `1958.*.js`).** `POST client/ticket` carries, per the
site's settings: `tstoken` — a **Cloudflare Turnstile** token when `ClientSiteEnableTurnstile`
is on (site key `0x4AAAAAABuZR-rUhagJ00LB`); a phone-verification round trip
(`client/phoneverify` → SMS code) when `ClientSiteValidate` is on; an **AWS captcha** whenever the
server answers `CAPTCHA_REQUIRED`; and a `FormToken` derived from a location key in app state. The
first three exist precisely to stop third parties auto-joining people, so a silent server-side
join is out even though the API is reachable. Whether RSF has Turnstile / phone-validate ON is
**unverified** (needs the state read above, or a look at the form on a phone).

**No prefill.** The join page reads only `c` (campaign), `cat`, `h`, `qid` from the URL — no
phone/name params. The deep link is `/join/48` (straight to the form); the phone is the one thing
they type. Waitwell itself remembers it in the browser after the first join.

**Consequence (per the brief's own rule):** `client.join()` raises `QueueUnavailable` by
construction — on the challenge (403 + `cf-mitigated`), on any non-2xx, and on a changed
response shape — and the gym beat falls back to **D1**: the join-form link with the user's
walking time. Series §2.8 (2026-09-19) adds the reactive version: a short "heading to the gym"
while the line's on gets D1 in code before any model turn (`gym_beats.heading_out`), gated on the
METER flag; mixed texts get the same link through the model's RSF context block. The client keeps the D2 contract (`join`/`status`/`leave`, `queue_tickets`
table, one open ticket per user) so that when a transport exists — Waitwell's own API, a
RecWell-provided integration (founder's heads-up email), or a headless-browser bridge if we
ever decide that's acceptable — it is a transport swap, not a rebuild.

**Not attempted:** headless-browser challenge solving. It's brittle, arguably against the
site's intent, and the brief classifies it under "can't be reproduced server-side".

**Observed `est_wait`:** none read yet (see the unfinished read path above) → `line_on` is inferred from `pct >= 95`.

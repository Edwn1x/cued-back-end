# Investigation — RSF line, location on request, receipts (2026-09-14)

Per the playbook: read the real code and the real external surfaces before building.
Spec = hypothesis; this is what's actually there.

## §1 Receipts

- **What's there now.** A legacy receipt handler exists (`agents/nutrition.handle_receipt_photo`,
  reached only via the legacy orchestrator when `classify_message` sees a receipt KEYWORD in the
  caption). The single-agent loop — the live path — sends every image to the model with the
  meal-estimation prompt and lets the model route in-call (food / calendar / whiteboard / other).
  There is no image pre-classifier, no `pantry` table, no receipt itemization. The memory layer has
  a TTL-aged `food_on_hand` category the extractor sometimes routes groceries into.
- **USDA.** `usda.search_usda(query)` → per-100 g macros (calories, protein_g, carbs_g, fat_g),
  raises `UsdaUnavailable`; needs `USDA_API_KEY` (set in prod). Good enough to map receipt lines to
  protein-per-100 g; quantities still need a grams estimate (the model's job, per playbook: the model
  estimates, code multiplies).
- **What it becomes.** A cheap pre-classifier on the image turn (haiku, one token), a receipt
  extractor (JSON), `pantry` upsert + `signals` row, a code-built reply
  (`got your <store> receipt. logged <3 items> — you're stocked through <weekday>.`), deterministic
  text depletion/inventory, and a `## PANTRY` context block. Flag `RECEIPTS_ENABLED`, default off.

## §2 RSF crowd meter + virtual line

- **Crowd meter.** The recwell page embeds
  `https://safe.density.io/#/displays/dsp_956223069054042646?token=shr_…` (weight room) and a second
  display for CMS. The scrapers (mashimar5/rsf-dashboard `density.py`) document the exchange:
  `POST https://identity.density.io/oauth/wayfinding/exchange` with `Authorization: Bearer <share
  token>` → `{access_token}`; then `GET https://api.density.io/app/v2/safe-display-core/displays/
  dsp_956223069054042646` with the access token → `dedicated_space.current_count / capacity`.
  Verified live today (see the probe output in the PR). Cap is 140. The count drifts through the day
  and resets overnight (sensor error). Public page updates ~every 10 min; we poll every 5 during hours.
- **Hours.** RSF hours page: Mon–Fri 7am–11pm, Sat 8am–6pm, Sun 8am–11pm; closed Thanksgiving,
  Christmas Eve/Day, New Year's Day. Parsed weekly with these as the fallback.
- **Waitwell.** `https://417804.waitwell.us/` and `/join/48` both answer **403 with a Cloudflare
  JS challenge** (`cf-mitigated: challenge`, `challenges.cloudflare.com`) to any non-browser client,
  including a mobile Safari User-Agent. The join form cannot be replayed server-side without a
  headless browser and a challenge solver — which is exactly the case the spec names:
  **`join` raises `QueueUnavailable` by construction; the beat falls back to D1** (the join-now
  link with the user's walking time). The FAQ confirms the product rules: line opens at ≥95%, join by
  phone or name, SMS updates, 10-minute window after summon, everyone admitted individually.
  D2 is built as a client whose transport is stubbed against a documented fixture, so the day
  Waitwell exposes an API (or RecWell offers one — founder's email) it's a transport swap.

## §3 Location

- **SDK.** spectrum-ts 12.8.0 (installed = latest on npm as of today). Neither the installed
  `@spectrum-ts/imessage` typings nor the newest on unpkg contain any `locations` API, and Photon's
  llms-full.txt has no location section. `imessage(app).locations.get(address)` **does not exist**
  in this SDK. Photon's pricing page lists "location sharing" as a feature, so it may be a
  dashboard/cloud capability not yet in the SDK.
- **Pins.** Unknown until the experiment: a Messages location pin is, on the wire, a vCard-ish
  attachment (`.loc.vcf`) or a Maps URL. The SDK converts vCard mime types to `contact` content —
  which our sidecar currently drops. The experiment logs the full inbound payload for the next
  message so we see the real shape.
- **Plan.** 3.0 only: sidecar `GET /location/:phone` returns the honest error (no SDK API) and the
  next inbound is logged in full. HARD STOP until Nau sends a pin.

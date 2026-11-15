# spectrum-sidecar

Cued's iMessage pipe. A single-file [Spectrum](https://photon.codes/docs/spectrum-ts)
(`spectrum-ts` 12.8, cloud iMessage provider) Bun service that:

- receives inbound iMessages over Photon's gRPC stream and forwards each one to
  Flask `POST /internal/inbound` (JSON, or multipart when there's an attachment);
- exposes a tiny private HTTP API Flask calls to send: `POST /send` (optional `reply_to`), `POST /react`, `POST /read`, `POST /typing`, `POST /contact-card`, `GET /health`.

It owns transport only. No coaching logic, no state. Send failures surface to
Flask as non-2xx so Flask's channel failover and the `delivery_status='failed'`
row (the keystone) happen there.

## Environment

| Var | What |
|---|---|
| `SPECTRUM_PROJECT_ID` / `SPECTRUM_PROJECT_SECRET` | Photon project creds (dashboard → Settings). `PROJECT_ID` / `PROJECT_SECRET` also accepted. |
| `INTERNAL_SHARED_SECRET` | Random 32+ chars. Same value on the Flask `web` service. Every route here requires it as `X-Internal-Secret`. |
| `FLASK_INTERNAL_URL` | Base URL of Flask, e.g. `http://web.railway.internal:8080`. |
| `PORT` | HTTP port, default `8080`. Binds `::` (dual-stack — Railway private networking is IPv6). |

## Run locally

```sh
bun install
bun run dev          # --watch
bun test             # unit tests (no Photon, no network)
bun run typecheck    # tsc --noEmit against the real SDK types
```

Gate checks (sidecar up, `INTERNAL_SHARED_SECRET=devsecret`):

```sh
curl localhost:8080/health -H "X-Internal-Secret: devsecret"

curl -X POST localhost:8080/send -H "X-Internal-Secret: devsecret" \
  -H "Content-Type: application/json" \
  -d '{"phone":"+12094205037","text":"sidecar says hi"}'
```

## HTTP API

All routes: `X-Internal-Secret` required, else `401`.

- `GET /health` → `200 {ok:true, lines:true, connected:true}`; `503` when the Spectrum stream is down.
- `POST /send` `{phone, text}` → `200 {ok:true, provider_message_id}`; `400` bad body; `502 {ok:false, error}` when Photon throws; `503` when disconnected.
- `POST /contact-card` `{phone}` → `200` / `502`. Shares the line's native contact card (best-effort).
- `POST /send-attachment` `{phone, name, mime_type, data_base64}` → `200 {ok:true, provider_message_id}`; `400` bad body / non-image mime (`image/gif|png|jpeg|webp` only) / over ~4.5 MB; `503` / `502` as above. One file as its own bubble, no caption (`attachment(bytes, {name, mimeType})`). Flask uses it for the water-break GIF (`water_gif.py`); the plain line is Flask's fallback when this fails.
- `POST /read` `{phone, message_id}` → `200` / `400` / `503` / `502`. Read receipt up to that message (remote iMessage marks the whole chat read). Flask fires it when reply generation begins, right before typing `start`, and when it thumbs-ups a suppressed ack.
- `POST /typing` `{phone, state?: "start"|"stop"}` → `200` / `400` / `503` / `502`. iMessage typing bubble in that DM (default `start`; `/send` also clears it). Flask fires `start` the moment reply generation begins — after the read-buffer, never during it — and `stop` on failure / SMS failover.
- `POST /poll` `{phone, title, options: string[]}` → `200 {ok, provider_message_id}` / `400` / `503` / `502`. An iMessage poll (iOS 26) as its own bubble via the SDK's `poll(title, ...options)`; 2–6 options, trimmed, none blank. The returned id is the poll's message guid — every later vote's id starts with it. Flask: `polls.py` (`send_poll` tool, `/admin/poll/send`).
- `POST /react` `{phone, message_id, emoji}` → `200` / `400` / `503` / `502`. Tapback on one of the user's messages (`emoji` = love|like|dislike|laugh|emphasize|question, or a raw emoji). `message_id` is the Photon id Flask stored on the inbound row; resolved via `space.getMessage`. `/send` accepts `reply_to` (same id) for a threaded reply. The coach decides WHEN on the Flask side — see `rewrite/proposals/reactions-and-threaded-replies.md`.

## Inbound → Flask

`POST {FLASK_INTERNAL_URL}/internal/inbound` with `X-Internal-Secret`.

- No attachment: JSON body
  `{phone, text, provider_message_id, chat_guid, service, line_phone, timestamp, attachments: []}`
- With attachment: `multipart/form-data` with a `payload` field (the same JSON,
  `attachments` filled with `{name, mime_type, size}`) and `attachment_0…N` file parts.
- 5xx / network errors retry (3 attempts, 1s/3s). 4xx is not retried.
- A tick whose build or forward throws (Photon upstream down — `attachment.read()`
  fails; live 2026-09-28) is parked in the inbound retry queue and re-tried on a
  5s/15s/30s/60s/120s/300s backoff. The last attempt forwards text-only with
  `attachments_unavailable: N` so the words still land; only then is it dropped
  (`inbound dropped after retries`). Logs: `inbound parked for retry`,
  `inbound retry ok`, `inbound retry failed`.
- Skipped, never forwarded: our own outbound echoes, non-iMessage platforms,
  typing / read receipts / polls / contact cards. (Reactions ARE forwarded — below.)
- `INBOUND_EXTRA_KINDS` (env, comma list, default empty) opens one more content kind at a
  time; unset = nothing changes on deploy, unsetting it is the rollback. Forwarded kinds
  are named in the payload's `kinds: [...]` (absent when none rode along).
  - `voice` — an iMessage audio message (`{type:"voice", mimeType: audio/*, read}` from
    the provider) is forwarded like a photo: bytes in `attachment_N` with the audio mime,
    plus `kinds: ["voice"]`. Flask stores `[voice memo attached]` (not a picture) and,
    until transcription lands, replies text-only and says it can't listen yet.
  - `poll_option` — a vote on a poll WE sent (`poll.changed` voted/unvoted → `{type:
    "poll_option", option, poll, selected, title}`, id `${pollGuid}:${sender}:${optionId}:
    ${selected|deselected}:${ms}`) is forwarded as a text inbound naming the pick
    (`[picked "5:30" on the poll "gym time today?"]`) plus `poll: {poll_id, title, option,
    selected, options}`. Flask stores and answers a pick like any reply; a deselect is
    dropped before any model turn.

## Allowlist (Free / Pro plans)

On shared-pool plans a phone number must exist in the Photon **Users** list before
`/send` to it will succeed; otherwise Photon returns `Target not allowed for this
project` and `/send` answers `502`. Flask adds users at signup (Phase 4C); until
then add them in the dashboard by hand.

## Mini-app cards (workout logger, Phase 0)

- `POST /send-card { phone, url, live? }` → `{ ok, provider_message_id, card_session }`.
  Sends `url` as an iMessage app card (`app(url, { live })`). `card_session` is the
  serializable handle (`id`, `miniAppCardSession`, `space`) Flask stores to update the
  card later. `live` defaults to `true`.
- `POST /update-card { phone, card_session, url }` → `{ ok }`. Edits the card in place
  (`edit(app(url, { live: true }), original)`). The original `Message` is kept in this
  process's memory; after a sidecar restart the target is rebuilt from `card_session`
  (the SDK notes refetched ids come back as inbound, so this is best-effort).
- Both behind `X-Internal-Secret`; provider refusals (tier, no extension) surface as
  `502 { error }` verbatim.

Phase 0 smoke test (from the Flask side): `GET /card/test` is a static 300px page.

## Inbound reactions (workout logger, Phase 5)

A tapback/emoji reaction on one of our messages is forwarded to Flask as a normal
inbound with `text: ""` and `reaction: { emoji, target_id }` (the reacted-to Photon
message id). Flask uses it for 👍 on a per-exercise workout message; anything else
is acknowledged and dropped.

## Location experiment (series §3.0)

- `GET /location/:phone` (secret) → **501** with the reason: spectrum-ts 12.8.0 has no
  `locations` API on the iMessage provider (nor do Photon's docs). Swap in the real call
  when the SDK grows one.
- `INBOUND_DEBUG_LOG=1` (env, experiment window only) logs the FULL shape of every inbound
  — content kinds, field names, attachment name/mime/size, the text, contact/richlink
  bodies — with the phone reduced to its last 4. Any content kind we don't forward is
  logged as `inbound skipped` with its type, outer type and field NAMES (always on).
- What the pinned provider (`@spectrum-ts/imessage` 12.8) actually emits inbound, read
  from its `buildUnwrappedContentMessage` / stream dispatch: `text`, `attachment`,
  `voice` (audio message), `contact` (vCard), `group` (multipart), `custom`
  (`imessage_type: "unsupported-message"`), each optionally inside `reply`; plus
  `reaction`, `read`, `poll_option` (a vote on a poll WE sent) and group-membership
  events. It never emits `effect`, `richlink`, `edit` or `unsend` inbound — an
  effect-wrapped text arrives as plain `text`; user edits/unsends have no stream event.

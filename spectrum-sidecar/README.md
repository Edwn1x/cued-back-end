# spectrum-sidecar

Cued's iMessage pipe. A single-file [Spectrum](https://photon.codes/docs/spectrum-ts)
(`spectrum-ts` 12.8, cloud iMessage provider) Bun service that:

- receives inbound iMessages over Photon's gRPC stream and forwards each one to
  Flask `POST /internal/inbound` (JSON, or multipart when there's an attachment);
- exposes a tiny private HTTP API Flask calls to send: `POST /send`, `POST /typing`, `POST /contact-card`, `GET /health`.

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
- `POST /typing` `{phone, state?: "start"|"stop"}` → `200` / `400` / `503` / `502`. iMessage typing bubble in that DM (default `start`; `/send` also clears it). Flask fires `start` the moment reply generation begins — after the read-buffer, never during it — and `stop` on failure / SMS failover.

## Inbound → Flask

`POST {FLASK_INTERNAL_URL}/internal/inbound` with `X-Internal-Secret`.

- No attachment: JSON body
  `{phone, text, provider_message_id, chat_guid, service, line_phone, timestamp, attachments: []}`
- With attachment: `multipart/form-data` with a `payload` field (the same JSON,
  `attachments` filled with `{name, mime_type, size}`) and `attachment_0…N` file parts.
- 5xx / network errors retry (3 attempts, 1s/3s). 4xx is not retried.
- Skipped, never forwarded: our own outbound echoes, non-iMessage platforms,
  reactions / typing / read receipts / polls.

## Allowlist (Free / Pro plans)

On shared-pool plans a phone number must exist in the Photon **Users** list before
`/send` to it will succeed; otherwise Photon returns `Target not allowed for this
project` and `/send` answers `502`. Flask adds users at signup (Phase 4C); until
then add them in the dashboard by hand.

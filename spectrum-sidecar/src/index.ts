/**
 * spectrum-sidecar — Cued's iMessage pipe (Photon Spectrum Cloud ⇄ Flask).
 *
 *   Photon Cloud ◀─gRPC stream─▶ this process ──JSON/multipart──▶ Flask /internal/inbound
 *                                     ▲
 *                                     └── POST /send {phone,text} ◀── Flask sms.py router
 *
 * Single file by design (handoff v2, Phase 1). The three pure pieces — HTTP
 * handler, inbound payload builder, Flask forwarder — are exported for tests;
 * the Spectrum stream and Bun.serve only start under `import.meta.main`.
 *
 * Discipline (ENGINEERING_PLAYBOOK §I): this process owns transport only. No
 * coaching logic, no state, no retries that could double-send. Errors on the
 * outbound path are surfaced to Flask as non-2xx so its failover + the
 * `delivery_status='failed'` row (the keystone) happen there, never hidden here.
 */

import { Emoji, Spectrum, reaction, reply, text, type Message, type Space } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";

// ─── small helpers ───────────────────────────────────────────────────────────

/** Log-safe handle: never a full phone/email in logs. */
export function last4(handle: string): string {
  return "…" + handle.slice(-4);
}

function log(level: "info" | "warn" | "error", msg: string, extra: Record<string, unknown> = {}) {
  const line = JSON.stringify({ t: new Date().toISOString(), level, msg, ...extra });
  (level === "error" ? console.error : level === "warn" ? console.warn : console.log)(line);
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ─── HTTP handler (Flask → sidecar) ──────────────────────────────────────────

export type Deps = {
  secret: string;
  /** Is the Spectrum stream currently up? Gates /health and /send. */
  connected: () => boolean;
  /** Open (or reuse) the DM with `phone` and send one text — as a threaded iMessage
   *  reply to `replyTo` (a Photon message id we stored on the inbound row) when given.
   *  Throws on failure, including an unknown `replyTo`. */
  send: (phone: string, body: string, replyTo?: string) => Promise<{ provider_message_id: string | null }>;
  /** Tapback / emoji reaction on the message `messageId` in the DM with `phone`.
   *  `emoji` is a tapback key (love|like|dislike|laugh|emphasize|question) or a raw
   *  emoji. Throws on failure, including an unknown message id. */
  react: (phone: string, messageId: string, emoji: string) => Promise<{ provider_message_id: string | null }>;
  /** Native "share name and photo" card into the DM with `phone`. Throws on failure. */
  shareContactCard: (phone: string) => Promise<void>;
  /** Read receipt: mark the DM with `phone` read up to `messageId` (one of THEIR
   *  messages; remote iMessage marks the whole chat read — same as a person's
   *  "Read 11:04"). Best-effort by contract. Throws on an unknown id. */
  read: (phone: string, messageId: string) => Promise<void>;
  /** iMessage typing indicator in the DM with `phone`: "start" shows the bubble,
   *  "stop" clears it. Best-effort by contract (the SDK no-ops where unsupported). */
  typing: (phone: string, state: TypingState) => Promise<void>;
};

export type TypingState = "start" | "stop";
const isTypingState = (v: unknown): v is TypingState => v === "start" || v === "stop";

/** The six iMessage tapbacks by name; anything else is sent as a raw emoji reaction. */
export const TAPBACKS: Record<string, string> = {
  love: Emoji.love, like: Emoji.like, dislike: Emoji.dislike,
  laugh: Emoji.laugh, emphasize: Emoji.emphasize, question: Emoji.question,
};
export function resolveEmoji(v: string): string {
  return TAPBACKS[v.trim().toLowerCase()] ?? v.trim();
}

async function readJson(req: Request): Promise<Record<string, unknown> | null> {
  try {
    const v = await req.json();
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const isNonEmptyString = (v: unknown): v is string => typeof v === "string" && v.length > 0;

export function createHandler(deps: Deps): (req: Request) => Promise<Response> {
  return async (req) => {
    if (req.headers.get("X-Internal-Secret") !== deps.secret) {
      return json(401, { ok: false, error: "unauthorized" });
    }
    const { pathname } = new URL(req.url);

    if (req.method === "GET" && pathname === "/health") {
      const up = deps.connected();
      // Line count isn't exposed by automatic discovery; `lines` is the boolean the handoff allows.
      return json(up ? 200 : 503, { ok: up, lines: up, connected: up });
    }

    if (req.method === "POST" && pathname === "/send") {
      const body = await readJson(req);
      if (!body || !isNonEmptyString(body.phone) || !isNonEmptyString(body.text)) {
        return json(400, { ok: false, error: "expected JSON {phone, text, reply_to?}" });
      }
      if (body.reply_to !== undefined && body.reply_to !== null && !isNonEmptyString(body.reply_to)) {
        return json(400, { ok: false, error: "reply_to must be a message id string" });
      }
      if (!deps.connected()) return json(503, { ok: false, error: "spectrum stream not connected" });
      try {
        const replyTo = isNonEmptyString(body.reply_to) ? body.reply_to : undefined;
        const { provider_message_id } = await deps.send(body.phone, body.text, replyTo);
        log("info", "send ok", { to: last4(body.phone), provider_message_id, chars: body.text.length, ...(replyTo ? { reply_to: replyTo } : {}) });
        return json(200, { ok: true, provider_message_id });
      } catch (err) {
        log("error", "send failed", { to: last4(body.phone), error: String(err) });
        return json(502, { ok: false, error: String(err) });
      }
    }

    if (req.method === "POST" && pathname === "/react") {
      // A tapback on one of the user's messages. The coach decides WHEN (prompt rules
      // + tools on the Flask side); this route only knows HOW. Target = the Photon
      // message id Flask stored on the inbound row; resolved via space.getMessage.
      const body = await readJson(req);
      if (!body || !isNonEmptyString(body.phone) || !isNonEmptyString(body.message_id) || !isNonEmptyString(body.emoji)) {
        return json(400, { ok: false, error: "expected JSON {phone, message_id, emoji}" });
      }
      if (!deps.connected()) return json(503, { ok: false, error: "spectrum stream not connected" });
      try {
        const { provider_message_id } = await deps.react(body.phone, body.message_id, body.emoji);
        log("info", "react ok", { to: last4(body.phone), on: body.message_id, emoji: resolveEmoji(body.emoji), provider_message_id });
        return json(200, { ok: true, provider_message_id });
      } catch (err) {
        log("error", "react failed", { to: last4(body.phone), on: body.message_id, error: String(err) });
        return json(502, { ok: false, error: String(err) });
      }
    }

    if (req.method === "POST" && pathname === "/read") {
      // Flask fires this the moment reply generation begins (right before typing
      // "start") and when it thumbs-ups a suppressed ack — so the user sees "Read"
      // before the dots, the way a person reads, then types, then sends.
      const body = await readJson(req);
      if (!body || !isNonEmptyString(body.phone) || !isNonEmptyString(body.message_id)) {
        return json(400, { ok: false, error: "expected JSON {phone, message_id}" });
      }
      if (!deps.connected()) return json(503, { ok: false, error: "spectrum stream not connected" });
      try {
        await deps.read(body.phone, body.message_id);
        log("info", "read", { to: last4(body.phone), upto: body.message_id });
        return json(200, { ok: true });
      } catch (err) {
        log("warn", "read failed", { to: last4(body.phone), upto: body.message_id, error: String(err) });
        return json(502, { ok: false, error: String(err) });
      }
    }

    if (req.method === "POST" && pathname === "/typing") {
      // Flask fires "start" the moment it begins generating a reply for an iMessage
      // user (after the read-buffer, not during it — a friend reads, then types) and
      // "stop" on any path where no iMessage reply will follow (error, SMS failover).
      // The bubble also clears itself when /send lands. Default state is "start".
      const body = await readJson(req);
      const state = body?.state ?? "start";
      if (!body || !isNonEmptyString(body.phone) || !isTypingState(state)) {
        return json(400, { ok: false, error: 'expected JSON {phone, state?: "start"|"stop"}' });
      }
      if (!deps.connected()) return json(503, { ok: false, error: "spectrum stream not connected" });
      try {
        await deps.typing(body.phone, state);
        log("info", "typing", { to: last4(body.phone), state });
        return json(200, { ok: true, state });
      } catch (err) {
        log("warn", "typing failed", { to: last4(body.phone), state, error: String(err) });
        return json(502, { ok: false, error: String(err) });
      }
    }

    if (req.method === "POST" && pathname === "/contact-card") {
      const body = await readJson(req);
      if (!body || !isNonEmptyString(body.phone)) return json(400, { ok: false, error: "expected JSON {phone}" });
      if (!deps.connected()) return json(503, { ok: false, error: "spectrum stream not connected" });
      try {
        await deps.shareContactCard(body.phone);
        return json(200, { ok: true });
      } catch (err) {
        log("error", "contact-card failed", { to: last4(body.phone), error: String(err) });
        return json(502, { ok: false, error: String(err) });
      }
    }

    return json(404, { ok: false, error: "not found" });
  };
}

// ─── inbound payload builder (Photon → sidecar) ──────────────────────────────

export type InboundPayload = {
  phone: string;
  text: string;
  provider_message_id: string;
  chat_guid: string;
  service: string | null;
  line_phone: string | null;
  timestamp: string;
  attachments: { name: string; mime_type: string; size: number | null }[];
};
export type InboundFile = { name: string; mimeType: string; bytes: Uint8Array };

/**
 * Turn one `[space, message]` tick into the Flask payload. Returns null for
 * anything we deliberately don't forward (our own echoes, other platforms,
 * reactions/typing/etc.). Reads attachment bytes exactly once.
 */
export async function buildInbound(
  space: Space,
  message: Message,
): Promise<{ payload: InboundPayload; files: InboundFile[] } | null> {
  if (message.direction === "outbound") return null;
  if (message.platform !== "imessage") return null;
  // iMessage sender extras (address/service) arrive via the provider's userSchema;
  // structural read keeps this testable without a live platform narrow.
  const sender = message.sender as (typeof message.sender & { address?: string; service?: string }) | undefined;
  if (!sender?.id) return null;

  // Threaded replies wrap the real content; unwrap one level.
  let content: unknown = message.content;
  if ((content as { type?: string }).type === "reply") {
    content = (content as { content: unknown }).content;
  }
  const c = content as
    | { type: "text"; text: string }
    | { type: "attachment"; id: string; name: string; mimeType: string; size?: number; read: () => Promise<Uint8Array> }
    | { type: string };

  let body = "";
  const files: InboundFile[] = [];
  if (c.type === "text") {
    body = (c as { text: string }).text;
  } else if (c.type === "attachment") {
    const a = c as { name: string; mimeType: string; size?: number; read: () => Promise<Uint8Array> };
    files.push({ name: a.name, mimeType: a.mimeType, bytes: await a.read() });
  } else {
    return null; // reaction, typing, read, poll, richlink, … — not coaching input
  }

  return {
    payload: {
      phone: sender.address ?? sender.id,
      text: body,
      provider_message_id: message.id,
      chat_guid: space.id,
      service: sender.service ?? null,
      line_phone: (space as { phone?: string }).phone ?? null,
      timestamp: message.timestamp.toISOString(),
      attachments: files.map((f) => ({ name: f.name, mime_type: f.mimeType, size: f.bytes.byteLength || null })),
    },
    files,
  };
}

// ─── Flask forwarder ─────────────────────────────────────────────────────────

export type ForwardConfig = {
  flaskUrl: string;
  secret: string;
  fetchImpl?: typeof fetch;
  /** Waits between attempts; length+1 = max attempts. Default 3 attempts. */
  backoffMs?: number[];
  timeoutMs?: number;
};

/**
 * POST the payload to Flask's /internal/inbound. JSON when there are no files,
 * multipart (`payload` JSON field + `attachment_N` parts) when there are.
 * Retries 5xx/network errors (default 3 attempts); 4xx is returned as-is —
 * Flask rejected it on purpose and a retry would say the same thing.
 */
export async function forwardInbound(
  payload: InboundPayload,
  files: InboundFile[],
  cfg: ForwardConfig,
): Promise<Response> {
  const doFetch = cfg.fetchImpl ?? fetch;
  const backoff = cfg.backoffMs ?? [1000, 3000];
  const url = cfg.flaskUrl.replace(/\/+$/, "") + "/internal/inbound";

  const build = (): RequestInit => {
    const headers: Record<string, string> = { "X-Internal-Secret": cfg.secret };
    if (files.length === 0) {
      headers["Content-Type"] = "application/json";
      return { method: "POST", headers, body: JSON.stringify(payload) };
    }
    const form = new FormData();
    form.set("payload", JSON.stringify(payload));
    files.forEach((f, i) =>
      form.set(`attachment_${i}`, new Blob([new Uint8Array(f.bytes)], { type: f.mimeType }), f.name));
    return { method: "POST", headers, body: form };
  };

  let lastErr: unknown = null;
  for (let attempt = 0; attempt <= backoff.length; attempt++) {
    try {
      const res = await doFetch(url, { ...build(), signal: AbortSignal.timeout(cfg.timeoutMs ?? 20_000) });
      if (res.status < 500) return res; // 2xx, or a deliberate 4xx — don't retry
      lastErr = new Error(`flask ${res.status}`);
    } catch (err) {
      lastErr = err;
    }
    if (attempt < backoff.length) await sleep(backoff[attempt]!);
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

// ─── bootstrap ───────────────────────────────────────────────────────────────

function requireEnv(...names: string[]): string {
  for (const n of names) {
    const v = process.env[n];
    if (v) return v;
  }
  log("error", `missing required env: ${names.join(" or ")}`);
  process.exit(1);
}

async function main() {
  const projectId = requireEnv("SPECTRUM_PROJECT_ID", "PROJECT_ID");
  const projectSecret = requireEnv("SPECTRUM_PROJECT_SECRET", "PROJECT_SECRET");
  const secret = requireEnv("INTERNAL_SHARED_SECRET");
  const flaskUrl = requireEnv("FLASK_INTERNAL_URL");
  const port = Number(process.env.PORT ?? 8080);

  type App = Awaited<ReturnType<typeof connect>>;
  let current: App | null = null;

  const connect = () =>
    Spectrum({ projectId, projectSecret, providers: [imessage.config()] });

  const dmFor = async (app: App, phone: string) => {
    const im = imessage(app);
    const user = await im.user(phone);
    return im.space.create(user); // 1:1 DM; shared-pool routes the line automatically
  };

  const deps: Deps = {
    secret,
    connected: () => current !== null,
    send: async (phone, body, replyTo) => {
      const app = current;
      if (!app) throw new Error("spectrum stream not connected");
      const dm = await dmFor(app, phone);
      let content = text(body);
      if (replyTo) {
        const target = await dm.getMessage(replyTo);
        if (!target) throw new Error(`reply_to message not found: ${replyTo}`);
        content = reply(content, target);
      }
      const sent = await dm.send(content);
      // iMessage clears the typing bubble when a message lands; this is the belt
      // to that suspenders — never let a stale "typing…" outlive the reply.
      await dm.stopTyping().catch(() => undefined);
      return { provider_message_id: (Array.isArray(sent) ? sent[0]?.id : sent?.id) ?? null };
    },
    react: async (phone, messageId, emoji) => {
      const app = current;
      if (!app) throw new Error("spectrum stream not connected");
      const dm = await dmFor(app, phone);
      const target = await dm.getMessage(messageId);
      if (!target) throw new Error(`message not found: ${messageId}`);
      const sent = await dm.send(reaction(resolveEmoji(emoji), target));
      return { provider_message_id: sent?.id ?? null };
    },
    read: async (phone, messageId) => {
      const app = current;
      if (!app) throw new Error("spectrum stream not connected");
      const dm = await dmFor(app, phone);
      const target = await dm.getMessage(messageId);
      if (!target) throw new Error(`message not found: ${messageId}`);
      await dm.read(target);
    },
    typing: async (phone, state) => {
      const app = current;
      if (!app) throw new Error("spectrum stream not connected");
      const dm = await dmFor(app, phone);
      if (state === "start") await dm.startTyping();
      else await dm.stopTyping();
    },
    shareContactCard: async (phone) => {
      const app = current;
      if (!app) throw new Error("spectrum stream not connected");
      const dm = await dmFor(app, phone);
      await imessage(dm).shareContactCard();
    },
  };

  // Railway private networking is IPv6-only; '::' binds dual-stack.
  Bun.serve({ hostname: "::", port, fetch: createHandler(deps) });
  log("info", "http listening", { port, hostname: "::", flask: flaskUrl });

  // Stream loop with reconnect-and-backoff. One thrown error must never end
  // the process; a per-message failure (Flask down) is logged and the stream
  // keeps draining so the next inbound isn't lost behind it.
  let backoffMs = 1000;
  for (;;) {
    let app: App | null = null;
    try {
      app = await connect();
      current = app;
      backoffMs = 1000;
      log("info", "spectrum connected", { providers: ["imessage"] });

      for await (const [space, message] of app.messages) {
        // Every inbound tick, by TYPE (never body): live 2026-09-11 the founder sent a
        // calendar screenshot and this process logged nothing — a silent stream is
        // undiagnosable. This line tells us whether Photon delivered it at all, and as what.
        if (message.direction !== "outbound") {
          const ct = (message.content as { type?: string } | undefined)?.type ?? "none";
          const inner = ct === "reply" ? ((message.content as { content?: { type?: string } }).content?.type ?? "none") : undefined;
          log("info", "stream tick", { id: message.id, platform: message.platform, content_type: ct, ...(inner ? { inner_type: inner } : {}) });
        }
        try {
          const built = await buildInbound(space, message);
          if (!built) continue;
          const { payload, files } = built;
          log("info", "inbound", {
            from: last4(payload.phone), id: payload.provider_message_id,
            chars: payload.text.length, attachments: files.length, service: payload.service,
          });
          const res = await forwardInbound(payload, files, { flaskUrl, secret });
          if (!res.ok) log("warn", "flask rejected inbound", { status: res.status, id: payload.provider_message_id });
        } catch (err) {
          log("error", "inbound forward failed", { error: String(err), id: message.id });
        }
      }
      log("warn", "spectrum stream ended; reconnecting");
    } catch (err) {
      log("error", "spectrum stream error", { error: String(err), retry_in_ms: backoffMs });
    } finally {
      current = null;
      if (app) await app.stop().catch(() => {});
    }
    await sleep(backoffMs);
    backoffMs = Math.min(backoffMs * 2, 60_000);
  }
}

if (import.meta.main) {
  main().catch((err) => {
    log("error", "fatal", { error: String(err) });
    process.exit(1);
  });
}

/**
 * Phase 1 sidecar — tier-1 style unit tests (no Photon, no network).
 *
 * `src/index.ts` exports its three pure pieces (HTTP handler, inbound payload
 * builder, Flask forwarder) and only boots the Spectrum stream under
 * `import.meta.main`, so importing it here has no side effects.
 */
import { describe, expect, test } from "bun:test";
import {
  buildInbound,
  createHandler,
  forwardInbound,
  last4,
  type Deps,
} from "./index.ts";

const SECRET = "devsecret";

function deps(overrides: Partial<Deps> = {}): Deps & { calls: unknown[][] } {
  const calls: unknown[][] = [];
  return {
    secret: SECRET,
    connected: () => true,
    send: async (phone, body) => {
      calls.push(["send", phone, body]);
      return { provider_message_id: "photon-msg-1" };
    },
    shareContactCard: async (phone) => {
      calls.push(["contact", phone]);
    },
    typing: async (phone, state) => {
      calls.push(["typing", phone, state]);
    },
    ...overrides,
    calls,
  };
}

function req(path: string, init: RequestInit & { secret?: string | null } = {}) {
  const { secret = SECRET, ...rest } = init;
  const headers = new Headers(rest.headers);
  if (secret !== null) headers.set("X-Internal-Secret", secret);
  if (rest.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return new Request(`http://sidecar${path}`, { ...rest, headers });
}

// ─── auth ────────────────────────────────────────────────────────────────────

describe("auth", () => {
  test("every route 401s without the shared secret", async () => {
    const h = createHandler(deps());
    for (const [m, p] of [["GET", "/health"], ["POST", "/send"], ["POST", "/contact-card"], ["POST", "/typing"]] as const) {
      const res = await h(req(p, { method: m, secret: null, body: m === "POST" ? "{}" : undefined }));
      expect(res.status).toBe(401);
      expect(await res.json()).toEqual({ ok: false, error: "unauthorized" });
    }
  });

  test("wrong secret is also 401", async () => {
    const res = await createHandler(deps())(req("/health", { secret: "nope" }));
    expect(res.status).toBe(401);
  });
});

// ─── /health ─────────────────────────────────────────────────────────────────

describe("GET /health", () => {
  test("200 when the Spectrum stream is connected", async () => {
    const res = await createHandler(deps())(req("/health"));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, lines: true, connected: true });
  });

  test("503 when not connected — a sidecar that can't send is not healthy", async () => {
    const res = await createHandler(deps({ connected: () => false }))(req("/health"));
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ ok: false, lines: false, connected: false });
  });
});

// ─── /send ───────────────────────────────────────────────────────────────────

describe("POST /send", () => {
  test("happy path returns the provider message id", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/send", {
      method: "POST", body: JSON.stringify({ phone: "+12094205037", text: "sidecar says hi" }),
    }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, provider_message_id: "photon-msg-1" });
    expect(d.calls).toEqual([["send", "+12094205037", "sidecar says hi"]]);
  });

  test("provider throw → 502 with the error text, never swallowed", async () => {
    const d = deps({ send: async () => { throw new Error("Target not allowed for this project"); } });
    const res = await createHandler(d)(req("/send", {
      method: "POST", body: JSON.stringify({ phone: "+15550001111", text: "x" }),
    }));
    expect(res.status).toBe(502);
    const body = (await res.json()) as { ok: boolean; error: string };
    expect(body.ok).toBe(false);
    expect(body.error).toContain("Target not allowed");
  });

  test("missing phone/text → 400", async () => {
    const h = createHandler(deps());
    for (const body of ["{}", JSON.stringify({ phone: "+1555" }), JSON.stringify({ text: "x" }), "not json"]) {
      const res = await h(req("/send", { method: "POST", body }));
      expect(res.status).toBe(400);
    }
  });

  test("503 when the stream is down — Flask's failover treats any non-2xx as failed", async () => {
    const d = deps({ connected: () => false });
    const res = await createHandler(d)(req("/send", {
      method: "POST", body: JSON.stringify({ phone: "+15550001111", text: "x" }),
    }));
    expect(res.status).toBe(503);
    expect(d.calls).toEqual([]);
  });
});

// ─── /contact-card ───────────────────────────────────────────────────────────

describe("POST /contact-card", () => {
  test("200 on success, 502 on throw (best-effort, but honest)", async () => {
    const ok = deps();
    let res = await createHandler(ok)(req("/contact-card", {
      method: "POST", body: JSON.stringify({ phone: "+15550001111" }),
    }));
    expect(res.status).toBe(200);
    expect(ok.calls).toEqual([["contact", "+15550001111"]]);

    const bad = deps({ shareContactCard: async () => { throw new Error("nope"); } });
    res = await createHandler(bad)(req("/contact-card", {
      method: "POST", body: JSON.stringify({ phone: "+15550001111" }),
    }));
    expect(res.status).toBe(502);
  });

  test("unknown route → 404", async () => {
    const res = await createHandler(deps())(req("/nope"));
    expect(res.status).toBe(404);
  });
});

// ─── inbound payload builder ─────────────────────────────────────────────────

function fakeMessage(over: Record<string, unknown> = {}) {
  return {
    id: "msg-guid-1",
    platform: "imessage",
    direction: "inbound",
    timestamp: new Date("2026-09-10T12:00:00Z"),
    sender: { id: "+12094205037", address: "+12094205037", service: "iMessage" },
    content: { type: "text", text: "hey coach" },
    ...over,
  } as never;
}
const fakeSpace = { id: "any;-;+12094205037", type: "dm", phone: "+15102646604" } as never;

// ─── POST /typing ────────────────────────────────────────────────────────────

describe("POST /typing", () => {
  test("start shows the bubble in the DM and is the default state", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/typing", { method: "POST", body: JSON.stringify({ phone: "+12094205037" }) }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, state: "start" });
    expect(d.calls).toEqual([["typing", "+12094205037", "start"]]);
  });

  test("stop clears it", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/typing", { method: "POST", body: JSON.stringify({ phone: "+12094205037", state: "stop" }) }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, state: "stop" });
    expect(d.calls).toEqual([["typing", "+12094205037", "stop"]]);
  });

  test("400 on a missing phone or an unknown state — and nothing is sent", async () => {
    const d = deps();
    for (const body of ["{}", JSON.stringify({ state: "start" }), JSON.stringify({ phone: "+1", state: "pause" }), "not json"]) {
      const res = await createHandler(d)(req("/typing", { method: "POST", body }));
      expect(res.status).toBe(400);
    }
    expect(d.calls).toEqual([]);
  });

  test("503 while the stream is down, 502 when the provider rejects", async () => {
    const down = deps({ connected: () => false });
    expect((await createHandler(down)(req("/typing", { method: "POST", body: JSON.stringify({ phone: "+1" }) }))).status).toBe(503);
    const bad = deps({ typing: async () => { throw new Error("Target not allowed"); } });
    const res = await createHandler(bad)(req("/typing", { method: "POST", body: JSON.stringify({ phone: "+1" }) }));
    expect(res.status).toBe(502);
    expect(((await res.json()) as { ok: boolean }).ok).toBe(false);
  });
});

describe("buildInbound", () => {
  test("text message → JSON payload, no files", async () => {
    const out = await buildInbound(fakeSpace, fakeMessage());
    expect(out).not.toBeNull();
    expect(out!.files).toEqual([]);
    expect(out!.payload).toEqual({
      phone: "+12094205037",
      text: "hey coach",
      provider_message_id: "msg-guid-1",
      chat_guid: "any;-;+12094205037",
      service: "iMessage",
      line_phone: "+15102646604",
      timestamp: "2026-09-10T12:00:00.000Z",
      attachments: [],
    });
  });

  test("outbound echo is skipped", async () => {
    expect(await buildInbound(fakeSpace, fakeMessage({ direction: "outbound" }))).toBeNull();
  });

  test("non-imessage platform is skipped", async () => {
    expect(await buildInbound(fakeSpace, fakeMessage({ platform: "terminal" }))).toBeNull();
  });

  test("attachment → bytes read once, metadata in payload, empty text", async () => {
    const bytes = Buffer.from([0xff, 0xd8, 0xff, 0xe0]);
    let reads = 0;
    const msg = fakeMessage({
      content: {
        type: "attachment", id: "att-1", name: "IMG_0001.jpeg", mimeType: "image/jpeg", size: 4,
        read: async () => { reads += 1; return bytes; },
      },
    });
    const out = await buildInbound(fakeSpace, msg);
    expect(reads).toBe(1);
    expect(out!.payload.text).toBe("");
    expect(out!.payload.attachments).toEqual([{ name: "IMG_0001.jpeg", mime_type: "image/jpeg", size: 4 }]);
    expect(out!.files).toHaveLength(1);
    expect(out!.files[0]!.name).toBe("IMG_0001.jpeg");
    expect(Buffer.from(out!.files[0]!.bytes).equals(bytes)).toBe(true);
  });

  test("threaded reply unwraps to its inner text", async () => {
    const msg = fakeMessage({ content: { type: "reply", content: { type: "text", text: "yes that one" }, target: {} } });
    const out = await buildInbound(fakeSpace, msg);
    expect(out!.payload.text).toBe("yes that one");
  });

  test("unsupported content (reaction, typing) is skipped", async () => {
    expect(await buildInbound(fakeSpace, fakeMessage({ content: { type: "reaction", emoji: "👍", target: {} } }))).toBeNull();
    expect(await buildInbound(fakeSpace, fakeMessage({ content: { type: "typing", state: "start" } }))).toBeNull();
  });

  test("no sender → skipped (nothing to route on)", async () => {
    expect(await buildInbound(fakeSpace, fakeMessage({ sender: undefined }))).toBeNull();
  });
});

// ─── Flask forwarder ─────────────────────────────────────────────────────────

const payload = {
  phone: "+12094205037", text: "hey", provider_message_id: "m1", chat_guid: "c1",
  service: "iMessage", line_phone: "+15102646604", timestamp: "2026-09-10T12:00:00.000Z", attachments: [],
};

function fakeFetch(statuses: number[]) {
  const seen: Request[] = [];
  const impl = async (input: Request | string | URL, init?: RequestInit) => {
    const r = input instanceof Request ? input : new Request(String(input), init);
    seen.push(r);
    const s = statuses[Math.min(seen.length - 1, statuses.length - 1)]!;
    return new Response(s === 200 ? '{"ok":true}' : "boom", { status: s });
  };
  return { impl: impl as typeof fetch, seen };
}
const cfg = (f: typeof fetch) => ({ flaskUrl: "http://web.railway.internal:8080", secret: SECRET, fetchImpl: f, backoffMs: [0, 0] });

describe("forwardInbound", () => {
  test("JSON POST to /internal/inbound with the shared-secret header", async () => {
    const f = fakeFetch([200]);
    const res = await forwardInbound(payload, [], cfg(f.impl));
    expect(res.status).toBe(200);
    expect(f.seen).toHaveLength(1);
    const r = f.seen[0]!;
    expect(r.url).toBe("http://web.railway.internal:8080/internal/inbound");
    expect(r.method).toBe("POST");
    expect(r.headers.get("X-Internal-Secret")).toBe(SECRET);
    expect(r.headers.get("Content-Type")).toContain("application/json");
    expect(await r.json()).toEqual(payload);
  });

  test("with files → multipart: `payload` JSON field + attachment_N parts", async () => {
    const f = fakeFetch([200]);
    const files = [{ name: "a.jpg", mimeType: "image/jpeg", bytes: Buffer.from([1, 2, 3]) }];
    await forwardInbound({ ...payload, attachments: [{ name: "a.jpg", mime_type: "image/jpeg", size: 3 }] }, files, cfg(f.impl));
    const r = f.seen[0]!;
    expect(r.headers.get("Content-Type")).toContain("multipart/form-data");
    const form = await r.formData();
    expect(JSON.parse(form.get("payload") as string).provider_message_id).toBe("m1");
    const part = form.get("attachment_0") as File;
    expect(part.name).toBe("a.jpg");
    expect(part.type).toBe("image/jpeg");
    expect(Buffer.from(await part.arrayBuffer())).toEqual(Buffer.from([1, 2, 3]));
  });

  test("retries a 5xx then succeeds", async () => {
    const f = fakeFetch([503, 200]);
    const res = await forwardInbound(payload, [], cfg(f.impl));
    expect(res.status).toBe(200);
    expect(f.seen).toHaveLength(2);
  });

  test("gives up after 3 attempts and throws — the caller logs, the stream lives on", async () => {
    const f = fakeFetch([500]);
    await expect(forwardInbound(payload, [], cfg(f.impl))).rejects.toThrow(/500/);
    expect(f.seen).toHaveLength(3);
  });

  test("a 4xx is NOT retried (Flask rejected it deliberately, e.g. 401 wrong secret)", async () => {
    const f = fakeFetch([401]);
    const res = await forwardInbound(payload, [], cfg(f.impl));
    expect(res.status).toBe(401);
    expect(f.seen).toHaveLength(1);
  });
});

test("last4 masks everything but the tail", () => {
  expect(last4("+12094205037")).toBe("…5037");
  expect(last4("me@example.com")).toBe("….com");
  expect(last4("")).toBe("…");
});

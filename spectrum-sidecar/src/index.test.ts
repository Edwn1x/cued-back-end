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
  resolveEmoji,
  type Deps,
} from "./index.ts";

const SECRET = "devsecret";

function deps(overrides: Partial<Deps> = {}): Deps & { calls: unknown[][] } {
  const calls: unknown[][] = [];
  return {
    secret: SECRET,
    connected: () => true,
    send: async (phone, body, replyTo) => {
      calls.push(replyTo ? ["send", phone, body, replyTo] : ["send", phone, body]);
      return { provider_message_id: "photon-msg-1" };
    },
    react: async (phone, messageId, emoji) => {
      calls.push(["react", phone, messageId, emoji]);
      return { provider_message_id: "photon-react-1" };
    },
    shareContactCard: async (phone) => {
      calls.push(["contact", phone]);
    },
    typing: async (phone, state) => {
      calls.push(["typing", phone, state]);
    },
    read: async (phone, messageId) => {
      calls.push(["read", phone, messageId]);
    },
    sendCard: async (phone, url, live, layout) => {
      calls.push(layout ? ["sendCard", phone, url, live, layout] : ["sendCard", phone, url, live]);
      return { provider_message_id: "photon-card-1",
               card_session: { id: "photon-card-1", miniAppCardSession: { chatGuid: "c", messageGuid: "m", sessionId: "s", targetMessageGuid: "t" }, space: { id: "sp", type: "dm", phone } } };
    },
    updateCard: async (phone, cardSession, url, live, layout) => {
      calls.push(layout || live !== undefined ? ["updateCard", phone, cardSession.id, url, live, layout] : ["updateCard", phone, cardSession.id, url]);
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
    for (const [m, p] of [["GET", "/health"], ["POST", "/send"], ["POST", "/contact-card"], ["POST", "/typing"], ["POST", "/react"], ["POST", "/read"]] as const) {
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

// ─── POST /react + threaded /send ────────────────────────────────────────────

describe("POST /react", () => {
  test("tapback by name on a stored message id", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/react", { method: "POST", body: JSON.stringify({ phone: "+12094205037", message_id: "spc-msg-abc", emoji: "laugh" }) }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, provider_message_id: "photon-react-1" });
    expect(d.calls).toEqual([["react", "+12094205037", "spc-msg-abc", "laugh"]]);
  });

  test("tapback names resolve to the six iMessage tapbacks; anything else is a raw emoji", () => {
    expect(resolveEmoji("love")).toBe("❤️");
    expect(resolveEmoji("LIKE ")).toBe("👍");
    expect(resolveEmoji("laugh")).toBe("😂");
    expect(resolveEmoji("emphasize")).toBe("‼️");
    expect(resolveEmoji("question")).toBe("❓");
    expect(resolveEmoji("dislike")).toBe("👎");
    expect(resolveEmoji("🔥")).toBe("🔥");
  });

  test("400 on missing fields; 503 down; 502 when the message id is unknown", async () => {
    const d = deps();
    for (const body of ["{}", JSON.stringify({ phone: "+1", emoji: "like" }), JSON.stringify({ phone: "+1", message_id: "x" })]) {
      expect((await createHandler(d)(req("/react", { method: "POST", body }))).status).toBe(400);
    }
    expect(d.calls).toEqual([]);
    expect((await createHandler(deps({ connected: () => false }))(req("/react", { method: "POST", body: JSON.stringify({ phone: "+1", message_id: "x", emoji: "like" }) }))).status).toBe(503);
    const bad = deps({ react: async () => { throw new Error("message not found: x"); } });
    const res = await createHandler(bad)(req("/react", { method: "POST", body: JSON.stringify({ phone: "+1", message_id: "x", emoji: "like" }) }));
    expect(res.status).toBe(502);
  });
});

describe("POST /send reply_to", () => {
  test("threads the text onto the given message id", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/send", { method: "POST", body: JSON.stringify({ phone: "+12094205037", text: "yeah 4 days is plenty", reply_to: "spc-msg-days" }) }));
    expect(res.status).toBe(200);
    expect(d.calls).toEqual([["send", "+12094205037", "yeah 4 days is plenty", "spc-msg-days"]]);
  });

  test("no reply_to → plain send, unchanged; a non-string reply_to is 400", async () => {
    const d = deps();
    await createHandler(d)(req("/send", { method: "POST", body: JSON.stringify({ phone: "+1", text: "hi", reply_to: null }) }));
    expect(d.calls).toEqual([["send", "+1", "hi"]]);
    const res = await createHandler(d)(req("/send", { method: "POST", body: JSON.stringify({ phone: "+1", text: "hi", reply_to: 42 }) }));
    expect(res.status).toBe(400);
  });
});

// ─── POST /read ──────────────────────────────────────────────────────────────

describe("POST /read", () => {
  test("marks the DM read up to their message", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/read", { method: "POST", body: JSON.stringify({ phone: "+12094205037", message_id: "spc-msg-1" }) }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    expect(d.calls).toEqual([["read", "+12094205037", "spc-msg-1"]]);
  });

  test("400 on missing fields; 503 down; 502 unknown id", async () => {
    const d = deps();
    for (const body of ["{}", JSON.stringify({ phone: "+1" }), JSON.stringify({ message_id: "x" })]) {
      expect((await createHandler(d)(req("/read", { method: "POST", body }))).status).toBe(400);
    }
    expect(d.calls).toEqual([]);
    expect((await createHandler(deps({ connected: () => false }))(req("/read", { method: "POST", body: JSON.stringify({ phone: "+1", message_id: "x" }) }))).status).toBe(503);
    const bad = deps({ read: async () => { throw new Error("message not found: x"); } });
    expect((await createHandler(bad)(req("/read", { method: "POST", body: JSON.stringify({ phone: "+1", message_id: "x" }) }))).status).toBe(502);
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

  test("a reaction is forwarded with its emoji and target id (workout tapbacks, Phase 5)", async () => {
    const built = await buildInbound(fakeSpace, fakeMessage({ content: { type: "reaction", emoji: "👍", target: { id: "spc-msg-ex-1" } } }));
    expect(built).not.toBeNull();
    expect(built!.payload.text).toBe("");
    expect(built!.payload.reaction).toEqual({ emoji: "👍", target_id: "spc-msg-ex-1" });
    const noTarget = await buildInbound(fakeSpace, fakeMessage({ content: { type: "reaction", emoji: "👍", target: {} } }));
    expect(noTarget!.payload.reaction).toEqual({ emoji: "👍", target_id: null });
  });

  test("unsupported content (typing, poll) is skipped", async () => {
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


// ─── mini-app cards (workout logger, Phase 0) ────────────────────────────────

describe("POST /send-card", () => {
  test("sends an app card and returns the id + serializable card_session", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/send-card", {
      method: "POST", body: JSON.stringify({ phone: "+12094205037", url: "https://web.example/card/test", live: true }),
    }));
    expect(res.status).toBe(200);
    const body = (await res.json()) as { ok: boolean; provider_message_id: string; card_session: { id: string; miniAppCardSession: unknown } };
    expect(body.ok).toBe(true);
    expect(body.provider_message_id).toBe("photon-card-1");
    expect(body.card_session.id).toBe("photon-card-1");
    expect(body.card_session.miniAppCardSession).toEqual({ chatGuid: "c", messageGuid: "m", sessionId: "s", targetMessageGuid: "t" });
    expect(d.calls).toEqual([["sendCard", "+12094205037", "https://web.example/card/test", true]]);
  });

  test("live defaults to true; live:false is passed through", async () => {
    const d = deps();
    const h = createHandler(d);
    await h(req("/send-card", { method: "POST", body: JSON.stringify({ phone: "+1555", url: "https://x/y" }) }));
    await h(req("/send-card", { method: "POST", body: JSON.stringify({ phone: "+1555", url: "https://x/y", live: false }) }));
    expect(d.calls.map((c) => c[3])).toEqual([true, false]);
  });

  test("400 on a missing phone, a non-http url, or bad json", async () => {
    const h = createHandler(deps());
    for (const body of ["{}", JSON.stringify({ phone: "+1555" }), JSON.stringify({ url: "https://x" }),
                        JSON.stringify({ phone: "+1555", url: "javascript:alert(1)" }), "nope"]) {
      const res = await h(req("/send-card", { method: "POST", body }));
      expect(res.status).toBe(400);
    }
  });

  test("provider throw → 502 with the error text (tier / extension refusals surface verbatim)", async () => {
    const d = deps({ sendCard: async () => { throw new Error("mini apps require the Business plan"); } });
    const res = await createHandler(d)(req("/send-card", {
      method: "POST", body: JSON.stringify({ phone: "+1555", url: "https://x/y", live: true }),
    }));
    expect(res.status).toBe(502);
    expect(((await res.json()) as { error: string }).error).toContain("Business plan");
  });

  test("503 when the stream is down", async () => {
    const res = await createHandler(deps({ connected: () => false }))(req("/send-card", {
      method: "POST", body: JSON.stringify({ phone: "+1555", url: "https://x/y" }),
    }));
    expect(res.status).toBe(503);
  });
});

describe("POST /update-card", () => {
  const cs = { id: "photon-card-1", miniAppCardSession: { chatGuid: "c", messageGuid: "m", sessionId: "s", targetMessageGuid: "t" } };

  test("edits the card in place with the serialized session", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/update-card", {
      method: "POST", body: JSON.stringify({ phone: "+12094205037", card_session: cs, url: "https://web.example/card/test?v=2" }),
    }));
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
    expect(d.calls).toEqual([["updateCard", "+12094205037", "photon-card-1", "https://web.example/card/test?v=2"]]);
  });

  test("400 without a card_session id or a url", async () => {
    const h = createHandler(deps());
    for (const body of [JSON.stringify({ phone: "+1555", url: "https://x" }),
                        JSON.stringify({ phone: "+1555", card_session: {}, url: "https://x" }),
                        JSON.stringify({ phone: "+1555", card_session: cs })]) {
      const res = await h(req("/update-card", { method: "POST", body }));
      expect(res.status).toBe(400);
    }
  });

  test("provider throw → 502 (e.g. session expired after a restart)", async () => {
    const d = deps({ updateCard: async () => { throw new Error("mini app card edits require a miniAppCardSession from the original send"); } });
    const res = await createHandler(d)(req("/update-card", {
      method: "POST", body: JSON.stringify({ phone: "+1555", card_session: { id: "gone" }, url: "https://x/y" }),
    }));
    expect(res.status).toBe(502);
  });
});

describe("card session helpers", () => {
  test("serializeCardSession keeps only what edit() needs; rebuildCardTarget is outbound with the session", async () => {
    const { serializeCardSession, rebuildCardTarget } = await import("./index");
    const sent = { id: "m1", content: { type: "app" }, direction: "outbound", miniAppCardSession: { chatGuid: "c", messageGuid: "m", sessionId: "s", targetMessageGuid: "t" },
                   space: { id: "sp", type: "dm", phone: "+1555", extra: "dropped" }, sender: { id: "agent" } };
    const cs = serializeCardSession(sent)!;
    expect(cs).toEqual({ id: "m1", miniAppCardSession: sent.miniAppCardSession, space: { id: "sp", type: "dm", phone: "+1555" } });
    expect(serializeCardSession(undefined)).toBeNull();
    const t = rebuildCardTarget(cs) as unknown as { id: string; direction: string; miniAppCardSession: unknown; content: unknown };
    expect(t.id).toBe("m1");
    expect(t.direction).toBe("outbound");
    expect(t.miniAppCardSession).toEqual(sent.miniAppCardSession);
    expect(t.content).toBeTruthy();
  });
});


describe("card layout (static preview + overlay)", () => {
  test("/send-card passes a trimmed layout and live:false through", async () => {
    const d = deps();
    const res = await createHandler(d)(req("/send-card", {
      method: "POST", body: JSON.stringify({ phone: "+1555", url: "https://cued.fit/card.html?t=x", live: false,
        layout: { caption: "push · wed", subcaption: "4 sets bench, then the usual", trailingCaption: "0/13", junk: 1, image: "no" } }),
    }));
    expect(res.status).toBe(200);
    expect(d.calls).toEqual([["sendCard", "+1555", "https://cued.fit/card.html?t=x", false,
      { caption: "push · wed", subcaption: "4 sets bench, then the usual", trailingCaption: "0/13" }]]);
  });

  test("/update-card passes layout + live for the in-place caption refresh", async () => {
    const d = deps();
    const cs = { id: "photon-card-1", miniAppCardSession: { chatGuid: "c", messageGuid: "m", sessionId: "s", targetMessageGuid: "t" } };
    const res = await createHandler(d)(req("/update-card", {
      method: "POST", body: JSON.stringify({ phone: "+1555", card_session: cs, url: "https://cued.fit/card.html?t=x&v=2", live: false,
        layout: { caption: "push · wed", trailingCaption: "13/13 · 8,040 lb" } }),
    }));
    expect(res.status).toBe(200);
    expect(d.calls).toEqual([["updateCard", "+1555", "photon-card-1", "https://cued.fit/card.html?t=x&v=2", false,
      { caption: "push · wed", trailingCaption: "13/13 · 8,040 lb" }]]);
  });

  test("pickLayout drops non-strings and empty objects", async () => {
    const { pickLayout } = await import("./index");
    expect(pickLayout({ caption: "", summary: 5 })).toBeUndefined();
    expect(pickLayout("x")).toBeUndefined();
    expect(pickLayout({ caption: "a", subcaption: "b" })).toEqual({ caption: "a", subcaption: "b" });
  });
});


describe("GET /location/:phone (series §3.0 experiment b)", () => {
  test("answers 501 with the exact reason — no locations API in this SDK", async () => {
    const res = await createHandler(deps())(req("/location/%2B12094205037"));
    expect(res.status).toBe(501);
    const body = (await res.json()) as { ok: boolean; error: string };
    expect(body.ok).toBe(false);
    expect(body.error).toContain("spectrum-ts 12.8.0");
  });

  test("still behind the secret", async () => {
    const res = await createHandler(deps())(req("/location/%2B1555", { secret: "nope" }));
    expect(res.status).toBe(401);
  });
});

describe("inbound debug log (series §3.0 experiment a)", () => {
  test("an unknown content kind is logged as skipped, not dropped silently", async () => {
    const built = await buildInbound(fakeSpace, fakeMessage({ content: { type: "contact", user: undefined, raw: "BEGIN:VCARD" } }));
    expect(built).toBeNull();
  });
});

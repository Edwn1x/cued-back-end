# Cued

An AI fitness & nutrition coach that lives in your text messages. One agent, one
voice, one memory, tools, and a heartbeat — on a deterministic state layer.

> **Architecture note.** Cued was rebuilt from a multi-agent pipeline (classifier →
> specialists → merge, plus a coach monolith and templated schedulers) into a single
> agent on stronger deterministic state. The rewrite shipped in seven phases
> (`rewrite/phase-*/`), each flag-gated; the legacy pipeline is retired behind flags and
> deleted once the live gates go green (see `rewrite/phase-6/INVESTIGATION.md`).
> Principle throughout: **deterministic state, model decisions.**

## How it works

```
Twilio SMS ─▶ /webhook ─▶ message buffer (debounce) ─▶ process_buffered_message
                                                           │
                                    onboarding? ─▶ onboarding_agent
                                                           │
                                                  run_agent_loop  ◀── the one agent
                                                           │
   build_loop_context (unified memory + events + split pointer + today's meals + …)
                                                           │
                       model turn ⇄ tools (agent_tools.dispatch_tool)
                                                           │
                                                     send_sms (GSM-7)
```

- **The agent** (`agent_loop.py`) gets one unified context and one voice
  (`prompts/voice.md`), and drives a tool-execution loop. State writes are
  **code-mediated**: the model requests, code validates + writes under a row lock.
- **Tools** (`agent_tools.py`): `remember`, `log_workout`, `manage_log` (soft-delete),
  `log_meal` (read-before-write), `get_dining_menu`, plus server-side `web_search` and
  model-routed `read_image`.
- **Memory** (`memory.py`): a categorized profile with validity windows + a safety
  floor; maintained nightly by `consolidation.py` (close stale / merge dupes / collapse
  contradictions, with a bounded-delta abort + rollback + a human-readable audit line)
  and enriched by `episodic.py` (dated life-context notes when a conversation goes quiet).
- **The heartbeat** (`heartbeat.py`): a jittered clock + a per-user "say something or
  stay silent?" decision (default silent), with guardrails enforced in code.
- **Deterministic state** (`models.py`, `split_pointer.py`, `events.py`): soft-delete
  chokepoint, local-day windowing, split pointer with provenance, webhook idempotency.

Everything new is behind an `os.getenv` flag in `config.py` (default off).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in Twilio + Anthropic creds
python migrate.py             # apply schema (idempotent; Postgres in prod)
python app.py                 # runs on :5000
```

Point your Twilio number's inbound webhook at `https://<host>/webhook` (POST). For
local dev, `ngrok http 5000` and use the https URL.

Endpoints: `GET /` health · `POST /webhook` Twilio inbound · `GET /signup` ·
`GET /admin` dashboard · `POST /admin/send` manual override.

## Rollout flags (config.py)

| Flag | Turns on |
|---|---|
| `SINGLE_AGENT_LOOP_ENABLED` | the single agent (else legacy pipeline) |
| `REMEMBER_/LOG_WORKOUT_/MANAGE_LOG_/LOG_MEAL_/GET_DINING_MENU_/WEB_SEARCH_/READ_IMAGE_*` | each tool |
| `HEARTBEAT_ENABLED` + `HEARTBEAT_ALLOWLIST` | proactive heartbeat (burn-in on an allowlist) |
| `CONSOLIDATION_ENABLED` / `EPISODIC_ENABLED` | nightly memory maintenance / episodic digest |

Launch order: flip the loop + tools → allowlist your number → `HEARTBEAT_ENABLED` →
`CONSOLIDATION_ENABLED` + `EPISODIC_ENABLED` → run a week, read the morning
consolidation audit lines.

## Testing

Two tiers (`tests/`):
- **tier-1** — deterministic, runs a disposable **Postgres 18** cluster (matches prod, so
  `FOR UPDATE` / `flag_modified` / timestamp semantics are real, not SQLite no-ops).
  `pytest tests/tier1`.
- **tier-2** — live, model-judged; metered, gated on a funded key.
  `pytest --run-tier2`.

The Anthropic SDK is faked centrally in tier-1 (`tests/_fake_anthropic.py`); the model
strings live in `config.py` (Sonnet-class loop, Haiku-class gates).

## Deployment

Railway + managed Postgres. Set env vars in the dashboard; migrations run automatically
at container start (`Procfile`: `python migrate.py && python app.py`) before the app
serves traffic, so a manual `python migrate.py` step is no longer part of the deploy —
a failed migration now blocks boot instead of racing a half-migrated live app. Cost at
beta scale is dominated by Twilio SMS + Claude tokens (~$0.007 / normal reply; a
searching turn ~$0.08).

## Project structure

```
app.py             Flask webhook / signup / admin; buffered-message routing
agent_loop.py      the single agent + build_loop_context
agent_tools.py     tool definitions + code-mediated dispatch
heartbeat.py       proactive tick engine (guardrails + decision)
consolidation.py   nightly memory consolidation (bounded-delta, rollback, audit)
episodic.py        episodic digest (quiet-conversation life-context notes)
memory.py          categorized profile, validity windows, safety floor
models.py          SQLAlchemy models + soft-delete chokepoint + accessors
events.py          local-day episodic event floor
split_pointer.py   training-split pointer with provenance
scheduler.py       APScheduler wiring (heartbeat + nightly jobs; legacy touchpoints retiring)
engagement_tracker.py  outbound-gating helpers (has_unanswered_outbound, tiers)
onboarding_agent.py    onboarding flow
sms.py / sms_encoding.py   Twilio send + GSM-7 normalization
config.py          all flags + model strings
migrate.py         idempotent migrations
prompts/voice.md   the one voice
rewrite/phase-*/   per-phase INVESTIGATION / CHANGESPEC / SUMMARY (async review)
```

Legacy (behind flags, deleted in Phase 6 once gates go green): `orchestrator.py`,
`agents/`, `coach.py`, `skill_loader.py`, `tone_analyzer.py`, per-turn extraction.

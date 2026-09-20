# Investigation — waitlist with a full profile + iMessage opt-in up front (2026-09-19)

Per the playbook: read the real code before building; spec = hypothesis. This is what's
there, what it becomes, why, and where. Companion site change: `cued-site` replaces the
three-field waitlist modal on `index.html` with the chat sign-up overlay, posting here.

## §1 What's there now

- **`POST /waitlist`** (`app.py`) reads name, phone (strict E.164), optional email, source,
  timezone. Everything else in the body is dropped. Writes a `User` with
  `waitlist_status='pending'`, `onboarding_step=0`. No SMS, no Photon. Rate-limited 3/min.
- **`POST /signup`** (the chat overlay's route) reads the full profile — age, gender, goal
  (list → csv), biggest_obstacle, experience, equipment — requires `sms_consent`, provisions
  the Photon user synchronously (`photon.provision_user`, flag-gated, never raises), returns
  `imessage_link`, and DEFERS the hook while a link exists (iMessage-first, 2026-09-14).
- **Promotion** — `POST /admin/user/<id>/activate-waitlist` clears `waitlist_status`, stamps
  `activated_at`, calls `start_onboarding` → `send_onboarding_hook` → `provision_user`
  (idempotent) → `send_sms`. For an un-opted-in shared Photon user the first blue send hits
  the consent gate and falls over to a Twilio SMS with the opt-in link appended
  (`sms._with_imessage_invite`, onboarding type only). So promotion already works; the
  first text is just green, with a "tap this" link.
- **The gate the site needs before it can ship:** nothing in the inbound pipeline checks
  `waitlist_status`. A pending user who texts the line today is resolved by phone in
  `/webhook` or `/internal/inbound`, dispatched into `_process_inbound`, buffered, and
  reaches `process_buffered_message` → the model coaches them. Worse, if they were
  provisioned (`photon_user_id` set, `preferred_channel='imessage'`, step 0, no outbound),
  `awaiting_channel_choice` is True and their first text SENDS THE HOOK — onboarding starts
  from the waitlist. `send_fallback_hooks` (scheduler) has the same hole: the same shape
  older than `ONBOARDING_HOOK_FALLBACK_MINUTES` gets the hook by SMS.
- **`/signup/channel`** ("I don't have an iPhone") flips `preferred_channel='sms'` and sends
  the hook if step 0 — again, it would start a pending user.
- **Admin waitlist tab** (`admin_dashboard.py`) shows name / phone / email / source / tz /
  joined + Activate. No profile columns, no channel state.
- **Model** has every profile column already (`User.age … equipment`). No column records
  that a number has texted its line (opt-in is only inferable from an inbound `Message`
  with `channel='imessage'`).
- **Tests**: `tests/tier1/test_imessage_first_signup.py` is the pattern (fixtures
  `photon_on`, `imessage_on`, `sidecar_ok`, `_post_imessage`). Baseline on this worktree:
  668 passed, 67 skipped, 1 failing — `test_manage_log_edit.py::
  test_event_edit_start_time_local_and_reflected_in_context`, which passes in isolation
  (order/time-dependent, pre-existing, untouched here).

## §2 What it becomes

1. **`/waitlist` stores the profile and provisions the line.** Accepts the same body the
   chat overlay sends `/signup`: `age, gender, goal (list|csv), biggest_obstacle,
   experience, equipment, sms_consent` on top of the existing fields. `sms_consent` must be
   `true` (400 otherwise — the whole point is to text them). Over-length enum strings are
   rejected 400, never truncated silently. Same defaults as `/signup`. Then
   `photon.provision_user` (flag-gated, degrades to no link) and the response gains
   `imessage_link` (str|null). Still no hook, still `waitlist_status='pending'`.
2. **Waitlist gate in `_process_inbound`.** Right after the inbound is logged (and the
   breaker reset), a pending user gets a code-owned holding line — once — and the turn
   ends: no safety pass, no buffer, no model. `WAITLIST_HOLD_TEXT` lives in
   `onboarding_agent.py`; sent through `send_sms` (routes blue if they just texted the
   line). Second and later texts are logged and left silent (`WAITLIST_INBOUND_HELD`).
3. **`imessage_opted_in_at`** — new nullable `users` column, stamped in `_process_inbound`
   the first time an inbound arrives on `channel='imessage'` (any user, not only waitlist).
   Deterministic state for "their line is open"; the admin tab reads it.
4. **`awaiting_channel_choice`** returns False for a pending user; **`send_fallback_hooks`**
   filters `waitlist_status IS NULL`. Belt and braces around §2 — the hook can only ever
   go to an activated user.
5. **`/signup/channel`** on a pending user flips the channel and sends nothing
   (`hook_sent:false, waitlist:true`). The site uses one route for both flows.
6. **Activation unchanged in code.** Opted in → `start_onboarding` goes blue first try.
   Never tapped → today's SMS + link fallover. Chose SMS → SMS directly.
7. **Admin tab** gains age / gender / goals / experience / equipment / obstacle and a
   channel badge: `iMessage ✓` (opted in), `link sent` (provisioned, not yet texted), `SMS`
   (chose no iPhone), `—` (not provisioned).

8. **Full name, first-name address (2026-09-20).** The chat asks for the full name;
   `/waitlist` keeps it in `users.full_name` (admin, email later) and stores `name` as
   the FIRST token only, whatever the client sent — `name` is what the hook template
   ("yo {name}…"), every trigger prompt, the memory header and the transcript labels
   inject, and the coach must never address someone by their full name.

## §3 Why

The founder wants the profile from the first minute ("a running start for the coach")
and wants the waitlist → active transition to not depend on a link tap at the moment of
activation. Capturing the opt-in at waitlist time makes activation one click. The gate
(§2.2) is the load-bearing piece: without it the site change would start coaching pending
users. Every decision here is state in code, no model involvement.

## §4 Where

- `app.py`: `/waitlist`, `_process_inbound` (gate + opt-in stamp), `/signup/channel`,
  admin `waitlist_data`.
- `onboarding_agent.py`: `WAITLIST_HOLD_TEXT`, `awaiting_channel_choice`,
  `send_fallback_hooks`.
- `models.py` + `migrate.py`: `imessage_opted_in_at`.
- `admin_dashboard.py`: waitlist table columns + badge.
- `tests/tier1/test_waitlist_profile.py` (new, red first), `test_migrations.py`.

## §5 Out of scope (candidates, not built)

- A `sms_consent_at` audit column (neither route stores consent today).
- Enum validation of gender/experience/equipment values (`/signup` doesn't either).
- The Photon free-tier user cap: every waitlister now takes a slot at sign-up. Provisioning
  failure degrades to "no link" — the row is still saved — but the cap size is unverified.

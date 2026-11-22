"""
Intake in the loop (founder, 2026-11-21): the setup conversation is the agent loop's turn,
not a separate agent's.

Before this, every text from a user with onboarding_step < 3 went to onboarding_agent — an
interview state machine with a text generator bolted on: one extraction call to fill fields,
one generation call to write the next question, and ONE tool (web search). It could not act.
Live 2026-11-20, user 100 (Maddy): "can we go to imsg" → "i can't move us over from my end rn
so we're stuck here for now" (false — the link existed; the intake agent had no way to send
it). Same shape as Sarah (no card mid-setup), Theo (no channel move), every "it can't do X
during setup" bug. Each one got a code-owned detour (meals mid-setup, the re-engage food-first
window, the card setup, the connect offers) instead of giving intake the tools.

Now, flag-gated (INTAKE_IN_LOOP_ENABLED, INTAKE_IN_LOOP_USER_IDS for a staged rollout), a
mid-setup turn is:

  1. onboarding_agent.absorb_turn — everything code stored before any reply existed, exactly
     as before: stats in code, the extractor, the vague-sleep estimate, injuries from memory,
     a pasted routine, a day list, stated lifts, the step 1 → 2 advance. The STILL UNKNOWN
     list the reply is written against is the one AFTER this turn's answer landed.
  2. onboarding_agent.maybe_complete — nothing unknown (or they want the workout and the
     card-critical basics are in) → the reaction bubble + the code summary + the setup
     sequence, unchanged. Completion stays code-owned: every number in it is real.
  3. Otherwise run_agent_loop, with the SETUP block (context_block) in the per-turn context:
     what's known, what's still unknown, the one-question rule, what code does at the close,
     and the mode code picked (friend / big ask / bundle / card ask / privacy / continuation).
     Every tool the coach normally has is on the table — log_meal, set_reminder, web_search,
     send_connect_link, send_poll, the lot — minus the ones that only make sense once setup
     has closed (EXCLUDED_TOOLS: targets, cards, the program). The model logs the burrito AND
     asks the next question in the same line; no second "logged. back to setup" bubble.
  4. onboarding_agent.shape_reply — the same code-owned guards the intake agent's replies
     pass: the repeat-question window, the half-answered re-ask exemptions, the sleep
     clause, the always-ask backstop. Then send_sms as message_type "onboarding" (the turn
     count, the big-ask once-only, the re-engage sweep all read that type).

The loop raising → False, and app.py falls through to the intake agent for that turn, as the
meal detour always did. The old agent is untouched until live evidence says it can go.
"""
from __future__ import annotations

import logging
import threading

import config

logger = logging.getLogger("cued.intake")

# Tools that presume setup has closed: targets are computed by code at the close (never by
# the model mid-setup), the first card / program come from code once the basics are in, and
# the rest are coaching-time knobs. Everything else the coach has, intake has.
EXCLUDED_TOOLS = frozenset({
    "set_targets", "start_workout_session", "reset_workout_session", "swap_card_exercise",
    "set_lift_anchors", "adjust_program", "save_routine", "reconstruct_routine_from_history",
    "pause_training", "set_checkin_level", "set_food_logger", "set_day_reset",
    "send_stat_card", "schedule_rundown",
})

_turns: dict[int, dict] = {}     # user id → this turn's intake state (mode, unknowns, notes)
_lock = threading.Lock()


def enabled_for(user) -> bool:
    """The flag, narrowed by the allowlist when one is set; never for a pending waitlister
    (their texts are held upstream) and never once setup has closed."""
    if not getattr(config, "INTAKE_IN_LOOP_ENABLED", False):
        return False
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        return False
    if getattr(user, "waitlist_status", None) == "pending":
        return False
    ids = getattr(config, "INTAKE_IN_LOOP_USER_IDS", None) or ()
    if ids and getattr(user, "id", None) not in ids:
        return False
    return True


def begin_turn(user_id: int, state: dict) -> None:
    with _lock:
        _turns[user_id] = dict(state)


def end_turn(user_id: int) -> None:
    with _lock:
        _turns.pop(user_id, None)


def is_turn(user_id: int | None) -> bool:
    return user_id in _turns


def turn_state(user_id: int | None) -> dict | None:
    return _turns.get(user_id) if user_id is not None else None


def filter_tools(user_id: int | None, tools: list) -> list:
    """The loop's tool list minus EXCLUDED_TOOLS, only on an intake turn."""
    if not is_turn(user_id):
        return tools
    kept = [t for t in tools if (t.get("name") if isinstance(t, dict) else None) not in EXCLUDED_TOOLS]
    dropped = len(tools) - len(kept)
    if dropped:
        logger.info("INTAKE_TOOLS_FILTERED user=%s dropped=%d kept=%d", user_id, dropped, len(kept))
    return kept


# ─── the SETUP block ───────────────────────────────────────────────────────────

def _profile_lines(user) -> str:
    """What's on file, in the intake agent's words (its own prompt builder owns this list)."""
    try:
        from onboarding_agent import _build_system_prompt
        prompt = _build_system_prompt(user)
    except Exception as e:  # noqa: BLE001
        logger.warning("INTAKE_PROFILE_LINES_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return f"Name: {getattr(user, 'name', '') or '?'}"
    head = "## WHAT YOU KNOW ABOUT THEM (from signup + what they've told you)\n"
    tail = "\n\n## STILL UNKNOWN"
    i = prompt.find(head)
    j = prompt.find(tail, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        return f"Name: {getattr(user, 'name', '') or '?'}"
    return prompt[i + len(head):j].strip()


def _assume_rule(user) -> str:
    from onboarding_agent import _assume_student
    if _assume_student(user):
        return ("Assume what's safe to assume, and SAY the assumption. A number texting cued is a Berkeley "
                "student: \"im assuming ur a berkeley student? what major, and any part time job\" (name "
                "berkeley the first time — never just \"here\"). If they're working, at another school, or "
                "not a student, take it in stride and go from there.")
    return ("Assume what's safe to assume, and SAY the assumption — but NOT that they're a student. What's on "
            "file (their age, what they do) says this is a working adult: no \"im assuming ur a berkeley "
            "student\", no major, no classes, midterms, dining halls or the RSF. Their day is work — meetings, "
            "a commute, their own gym — so that is what you're curious about. If they tell you they ARE a "
            "student, take it in stride and go from there.")


def _mode_lines(user, state: dict) -> str:
    from onboarding_agent import (_plain, _PRIVACY_BLOCK, _BIG_ASK_GYM, _BIG_ASK_NEVER)
    mode = state.get("mode") or "friend"
    missing = state.get("missing") or []
    name = (user.name or "they").split()[0] if getattr(user, "name", None) else "they"
    note = state.get("assumption_note")
    if mode == "continuation":
        prev = (state.get("prev_coach") or "").strip().replace("\n", " / ")[:300]
        return (f"THIS TURN: {name} sent this before seeing your last reply — it's the tail of the thought you "
                f"were already answering. Your last reply already went: \"{prev}\". Its question is still on "
                f"the floor: do NOT ask it again in other words, and do not ask anything new. If this text "
                f"changes nothing, reply exactly [silent]. If it adds something worth a word (a correction, a "
                f"joke, an answer to your question after all), ONE short sentence, no question.")
    if mode == "privacy":
        return "THIS TURN: " + _PRIVACY_BLOCK + " One message, one paragraph, no greeting."
    if mode == "card_ask":
        items = ", ".join(f[1].split(" — ")[0] for f in (state.get("card_missing") or []))
        return (f"THIS TURN: they want their workout. You can't send it yet — code sends their first card the "
                f"moment these are in: {items}. React to what they said, say the card's coming once u have "
                f"those (your words), and ask for them in one breath — only these, nothing already known. "
                f"2-3 sentences.")
    if mode == "big_ask":
        asked = [f for f in missing if f[0] not in _BIG_ASK_NEVER]
        gym = [_plain(f, user) for f in asked if f[0] in _BIG_ASK_GYM]
        day = [_plain(f, user) for f in asked if f[0] not in _BIG_ASK_GYM]
        parts = []
        if gym:
            parts.append("about the gym: " + ", ".join(gym))
        if day:
            parts.append("and separately: " + ", ".join(day))
        hint = "; ".join(parts) or "nothing"
        return (f"THIS TURN (the one time a list is okay): react to the specific thing they said first — if they "
                f"asked what you need, that's your cue, no apology. Then ask them to drop the basics in ONE "
                f"text — {hint}. Those plain phrasings or plainer; the gym things in one breath, the other "
                f"things in their own clause. Not a numbered list. ONLY the things listed here. 3-4 sentences "
                f"max; make it feel like one ask.")
    if mode == "bundle":
        gaps = " and ".join(_plain(f, user) for f in missing[:2])
        return (f"THIS TURN: react to what they said (answer any question fully). You're basically done getting "
                f"to know them — ask about {gaps} in one short, natural line ('last thing' energy), both in one "
                f"breath if there are two. Not a form. 1-2 sentences.")
    # friend (default)
    unknown = ", ".join(f[1].split(" — ")[0] for f in missing) or "nothing"
    line = (f"THIS TURN: react to the specific thing they said — have a take, be curious. Then end with ONE "
            f"question that would tell you one of these you still don't know: {unknown} — the one that fits "
            f"what they said; if nothing fits, ask the first one plainly.")
    if note:
        line += f" {note} Say that guess in one clause; nothing to ask about sleep."
    return line


def context_block(user) -> str | None:
    """The SETUP block the loop sees on an intake turn (None otherwise)."""
    state = turn_state(getattr(user, "id", None))
    if state is None:
        return None
    # The loop is handed the row app.py loaded BEFORE this turn's answer was stored; the
    # block reads the row absorb_turn refreshed (this turn's stats, the pinned sleep guess).
    user = state.get("user_row") or user
    try:
        from onboarding_agent import _get_experience_context, _NOT_DONE_LINE, _NO_CAMPUS_FACTS_FROM_MEMORY
        name = (user.name or "they").split()[0] if getattr(user, "name", None) else "they"
        missing = state.get("missing") or []
        unknown = "\n".join(f"- {desc}" for _f, desc in missing) or "- nothing — you have what you need"
        card = ", ".join(f[1].split(" — ")[0] for f in (state.get("card_missing") or [])) or "their stats, training days, anything that hurts"
        return (
            f"## SETUP IN PROGRESS — {name} is new, this is still the first conversation\n"
            f"You said it in your first text: a few texts to get to know them, then you run them through how you "
            f"work, then their first workout. Nothing is computed yet: NO calorie or protein targets (no "
            f"'remaining', no daily number, no 'on track'), no card, no program. Code sends their numbers and their "
            f"first card the moment setup closes, and code decides when that is. {_NOT_DONE_LINE}\n\n"
            f"EXPERIENCE CALIBRATION\n{_get_experience_context(user)}\n\n"
            f"WHAT YOU KNOW ABOUT THEM (from signup + what they've told you)\n{_profile_lines(user)}\n\n"
            f"STILL UNKNOWN (learn these by caring about their day — never by listing them)\n{unknown}\n\n"
            f"HOW A SETUP REPLY WORKS\n"
            f"- Every reply engages the specific thing they just said FIRST — the class, the place, the food, the "
            f"feeling. Have a take. If they NAMED something specific (a course number always counts, a campus "
            f"place, a restaurant, an event), web_search it and use ONE detail from this semester, your words, "
            f"no links. {_NO_CAMPUS_FACTS_FROM_MEMORY}\n"
            f"- Then ask about ONE thing from STILL UNKNOWN, the way a friend asks it, for a reason. While STILL "
            f"UNKNOWN has entries, EVERY reply ends with ONE question from it — a reply with no question stalls "
            f"setup and puts the next move on them. A question that isn't on STILL UNKNOWN is a wasted text.\n"
            f"- One question by default. Two only when they're about the SAME thing and joined with 'and'. Never "
            f"a list, never 'a few things I need from you', never fields / profiles / forms / what you 'need'.\n"
            f"- Be concrete: 'u cooking most nights or is it dining hall', not 'real meals or quick stuff'.\n"
            f"- Never ask the same thing twice. RECENT CONVERSATION shows what you already asked; a vague answer "
            f"IS the answer — estimate, say so, move on. Sleep: ONE ask, as a rough range; a vague answer gets a "
            f"code-pinned guess you'll be handed — never invent wake/sleep clock times yourself.\n"
            f"- {_assume_rule(user)}\n"
            f"- If they ask you something, answer it first, fully. 'how'd you know?' / 'i already told you' → "
            f"it's in RECENT CONVERSATION or WHAT YOU KNOW; never claim you don't have something written there.\n"
            f"- Everything you can normally DO works right now, mid-setup, and you do it in the same reply: a "
            f"meal they mention or a photo → log_meal and say what it was with ~cal and protein (no targets to "
            f"compare against), 'remind me' → set_reminder, a link / a menu / the calendar connect link / a "
            f"poll / a search → the tool. Then keep setup moving: the one question still ends the reply. A "
            f"pasted routine, a stated lift ('i bench 135') and their stats are saved by code before you see "
            f"this — don't re-save them, don't ask them again.\n"
            f"- Never set targets, never send or start a workout card, never build or change a program during "
            f"setup — code does those once the basics are in. If they ask for a workout, say it's coming once u "
            f"have {card}, in your words. Never say a card is loading, glitching, should pop up, or already "
            f"went; never write a workout out as text.\n"
            f"- 1-3 short sentences, one paragraph, no `---`, no greeting — you already said hey. Decide your ONE "
            f"question before you write, then write the reply once.\n\n"
            f"{_mode_lines(user, state)}"
        )
    except Exception as e:  # noqa: BLE001 — a block must never break a turn
        logger.warning("INTAKE_CONTEXT_BLOCK_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return None


# ─── the turn ──────────────────────────────────────────────────────────────────

def handle_turn(user, combined_body: str, message_type: str, *, image_url=None, images=None,
                continuation: bool = False, inbound_id=None) -> bool:
    """One mid-setup turn through the loop. True = handled (reply sent or deliberately
    silent, or setup closed); False = the loop failed before anything went out → the
    caller falls through to the intake agent for this turn."""
    import onboarding_agent as oa
    from sms import send_sms
    from typing_indicator import typing_stop

    continuation = bool(continuation and config.ONBOARDING_CONTINUATION_FOLD_ENABLED)
    has_reminder_tool = bool(getattr(config, "REMINDERS_ENABLED", True))
    absorbed = oa.absorb_turn(user, combined_body, capture_reminder=not has_reminder_tool)
    if absorbed is None:
        return False
    user_row = absorbed.user_row
    missing_after = absorbed.missing_after
    prev_coach = absorbed.prev_coach
    remaining = [f[0] for f in missing_after]

    if oa.maybe_complete(absorbed, combined_body, continuation=continuation):
        logger.info("INTAKE_LOOP_COMPLETED user=%s", user_row.id)
        return True

    wants_workout, card_missing, _early = oa.close_state(missing_after, combined_body)
    mode = oa._intake_mode(combined_body, missing_after, oa._coach_turns(user_row.id),
                           big_ask_sent=oa._big_ask_sent(user_row.id))
    privacy = mode != "big_ask" and oa.is_privacy_question(combined_body)
    if continuation and prev_coach and not absorbed.assumption_note:
        mode = "continuation"
    elif wants_workout and card_missing:
        mode = "card_ask"
    elif privacy:
        mode = "privacy"
    if mode == "privacy":
        logger.info("ONBOARDING_PRIVACY_TURN user=%s text=%r", user_row.id, (combined_body or "")[:80])

    state = {"mode": mode, "missing": missing_after, "card_missing": card_missing,
             "assumption_note": absorbed.assumption_note, "prev_coach": prev_coach, "user_row": user_row}
    from agent_loop import run_agent_loop
    from agent_tools import pop_turn_state, REACTION_ONLY_SENTINEL
    begin_turn(user_row.id, state)
    try:
        text = run_agent_loop(user, combined_body, message_type, image_data=image_url,
                              image_data_list=images, continuation=continuation, message_id=inbound_id)
    except Exception as e:  # noqa: BLE001 — the intake agent answers this one, as before
        logger.error("INTAKE_LOOP_FAILED user=%s err=%s", user_row.id, e, exc_info=True)
        pop_turn_state(user_row.id)
        return False
    finally:
        end_turn(user_row.id)
    turn = pop_turn_state(user_row.id)
    if (text or "").strip().lower() == REACTION_ONLY_SENTINEL:
        text = ""
    out_type = oa.BIG_ASK_MESSAGE_TYPE if mode == "big_ask" else "onboarding"

    if mode == "continuation":
        # [silent] → the loop already put the 👍 up (its continuation path); a repeat of the
        # question on the floor → 👍 instead of the words (the intake agent's rule).
        if not text:
            logger.info("ONBOARDING_CONTINUATION_SUPPRESSED user=%s reason=silent still_unknown=%s path=loop",
                        user_row.id, remaining)
            typing_stop(user_row.id)
        elif oa.is_duplicate_question(text, prev_coach):
            logger.info("ONBOARDING_CONTINUATION_SUPPRESSED user=%s reason=dup_question reply=%r still_unknown=%s path=loop",
                        user_row.id, text[:120], remaining)
            oa._react_thumbs(user_row.id)
            typing_stop(user_row.id)
        else:
            send_sms(user_row.phone, text, user_id=user_row.id, message_type=out_type,
                     reply_to_sid=turn.get("reply_to"))
            logger.info("ONBOARDING_REPLY mode=continuation user=%s still_unknown=%s path=loop", user_row.id, remaining)
    elif not text:
        # Silence is not a setup reply: the loop's 👍 went up, but setup needs its question.
        if missing_after and not privacy:
            field = next((f for f in missing_after if f[0] not in oa._BIG_ASK_NEVER), missing_after[0])
            line = f"quick one: {oa._plain(field, user_row)}?"
            logger.info("ONBOARDING_QUESTION_APPENDED user=%s field=%s reply='' path=loop", user_row.id, field[0])
            send_sms(user_row.phone, line, user_id=user_row.id, message_type=out_type)
        else:
            typing_stop(user_row.id)
    else:
        shaped, action = oa.shape_reply(absorbed, text, mode=mode, privacy=privacy)
        if action == "thumbs":
            oa._react_thumbs(user_row.id)
            typing_stop(user_row.id)
        else:
            send_sms(user_row.phone, shaped, user_id=user_row.id, message_type=out_type,
                     reply_to_sid=turn.get("reply_to"))
            if action == "send":
                logger.info(f"ONBOARDING_REPLY mode={mode} user={user_row.id} still_unknown={remaining} path=loop"
                            f"{' sleep_estimated=True' if absorbed.assumption_note else ''}")
    # Stat cards the coach queued land after its line (none expected mid-setup, but the
    # tool list is the loop's).
    if turn.get("stat_cards"):
        try:
            from agent_tools import flush_stat_cards
            flush_stat_cards(user_row.id, turn)
        except Exception as e:  # noqa: BLE001
            logger.warning("INTAKE_STAT_CARDS_FAILED user=%s err=%s", user_row.id, e)
    # Onboarding turns are full of durable life facts — the same memory extraction normal
    # turns get (background, best-effort), with the coach's whole turn.
    try:
        import app as _app
        from onboarding_agent import coach_turn_text, latest_message_id  # noqa: F401
        _app.threading.Thread(target=_app.extract_and_store_memory,
                              args=(user_row.id, combined_body, text or ""), daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.warning("INTAKE_MEMORY_EXTRACT_FAILED user=%s err=%s", user_row.id, e)
    return True

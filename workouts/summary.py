"""finish_session — total volume (Σ actual_weight × actual_reps over done sets), PR
list, duration, per-exercise lines `name — 185×5 · 185×5 · 190×4 · 185×5`, and the
honest tap/text counts for the closer."""

from __future__ import annotations

from datetime import datetime, timezone

from models import get_session, SetLog, WorkoutSession
from workouts.prs import check_pr
from workouts.templates import day_label


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt(w) -> str:
    return f"{float(w):g}"


def session_prs(session, ws: WorkoutSession, sets: list[SetLog]) -> list:
    """At most one PR per exercise: this session's HEAVIEST done set (then most
    reps) — the set a lifter calls the PR — judged against PREVIOUS sessions only.
    (Weight-first, not e1RM-first: on the site's card 185×5 edges 190×4 on Epley,
    but the story is "190 × 4 is a PR".)"""
    best: dict = {}
    for s in sets:
        if s.done and s.actual_weight and s.actual_reps:
            cur = best.get(s.exercise)
            if cur is None or (float(s.actual_weight), int(s.actual_reps)) > (float(cur.actual_weight), int(cur.actual_reps)):
                best[s.exercise] = s
    prs = []
    for ex, s in best.items():
        pr = check_pr(session, ws.user_id, ex, float(s.actual_weight), int(s.actual_reps), exclude_session_id=ws.id)
        if pr:
            prs.append(pr)
    return prs


def summarize(session, ws: WorkoutSession) -> dict:
    sets = (session.query(SetLog).filter(SetLog.session_id == ws.id)
            .order_by(SetLog.id).all())
    # A done set is a done set: bodyweight rows carry weight 0 and must still count
    # (live 2026-09: the `and s.actual_weight` gate made a bodyweight session read as
    # 0 sets done). Volume only sums loaded sets.
    done = [s for s in sets if s.done and s.actual_reps]
    volume = int(round(sum(float(s.actual_weight or 0) * int(s.actual_reps) for s in done)))
    lines, seen = [], []
    for s in sets:
        if s.exercise not in seen:
            seen.append(s.exercise)
    for ex in seen:
        ex_done = [s for s in done if s.exercise == ex]
        if not ex_done:
            continue
        label = next(s.exercise_label for s in sets if s.exercise == ex)
        lines.append(f"{label} — " + " · ".join(
            f"{_fmt(s.actual_weight)}×{s.actual_reps}" if (s.actual_weight or 0) else f"{s.actual_reps}"
            for s in ex_done))
    prs = session_prs(session, ws, done)
    start, end = ws.started_at or ws.date, ws.finished_at or _utcnow()
    minutes = max(int(round((end - start).total_seconds() / 60)), 0) if start else 0
    taps = sum(1 for s in done if s.source == "card")
    texts = sum(1 for s in done if s.source == "text")
    tapbacks = sum(1 for s in done if s.source == "tapback")
    return {"template_key": ws.template_key, "weekday": (ws.date or start).strftime("%a").lower() if (ws.date or start) else "",
            "minutes": minutes, "lines": lines, "volume_lb": volume, "pr_count": len(prs),
            "prs": [p.message for p in prs], "sets_done": len(done), "sets_planned": len(sets),
            "taps": taps, "texts": texts, "tapbacks": tapbacks}


def format_summary(s: dict) -> str:
    """The site card as a text:
        push · wed · 48 min
        bench press — 185×5 · 185×5 · 190×4 · 185×5
        …
        9,240 lb total · 1 PR
    """
    head = f"{day_label(s['template_key'])} · {s['weekday']}" + (f" · {s['minutes']} min" if s["minutes"] else "")
    prs = f"{s['pr_count']} PR" + ("s" if s["pr_count"] != 1 else "")
    if s["volume_lb"]:
        tail = f"{s['volume_lb']:,} lb total · {prs}"
    else:  # bodyweight session: sets are the story, there is no load to total
        tail = f"{s['sets_done']} sets" + (f" · {prs}" if s["pr_count"] else "")
    return "\n".join([head, *s["lines"], tail])


def finish_session(session_id: int, *, now=None) -> dict:
    now = now or _utcnow()
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            raise ValueError("no such session")
        if ws.status != "done":
            ws.status = "done"
            ws.finished_at = now
        s = summarize(session, ws)
        ws.total_volume_lb = s["volume_lb"]
        ws.pr_count = s["pr_count"]
        session.commit()
        return s
    finally:
        session.close()

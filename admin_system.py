"""
Admin System Console — introspection-driven debug pages.

Design rule: nothing here hardcodes a list of tables, columns, feature flags,
or API call sites. Every page derives its content at request time from live
introspection — SQLAlchemy Base.metadata for the schema, the config module for
flags/settings, GROUP BYs over the data for breakdowns — so backend changes
(a new model, a new flag, a new token_usage site) show up in the console
without editing this file. The only hand-written layer is the small "job
health" semantics table, which says what each background system's freshness
means.

Pages:
  /admin/system            flags + job health + auto DB overview
  /admin/data/<table>      generic row browser for ANY table in Base.metadata
  /admin/heartbeat         tick decisions, speak rate, search budget
  /admin/consolidation     nightly runs (human summary + aborts) + episodic digests
  /admin/user/<id>/debug   everything the coach knows/decided about one user
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytz
from flask import Blueprint, Response, render_template_string, request
from sqlalchemy import DateTime, func, select
from sqlalchemy.orm.attributes import flag_modified

import config
from models import (
    Base,
    ConsolidationRun,
    EpisodicDigest,
    Event,
    HeartbeatTick,
    Meal,
    TokenUsage,
    User,
    Workout,
    active,
    get_session,
)

logger = logging.getLogger("cued.admin")

admin_system_bp = Blueprint("admin_system", __name__)


def _audit(tag, **kw):
    """Every admin write logs at WARNING with the ADMIN_ACTION prefix so Railway
    logs answer 'who changed this' — same discipline as SAFETY_INVALIDATION."""
    logger.warning("ADMIN_ACTION %s %s", tag,
                   " ".join(f"{k}={v}" for k, v in kw.items()))


def _json(payload, status=200):
    """jsonify that survives datetimes/sets inside job result dicts."""
    return Response(json.dumps(payload, default=str), status=status,
                    mimetype="application/json")

PST = pytz.timezone("America/Los_Angeles")


# ─── time helpers (DB stores naive UTC; admin reads PST) ───────────────────

def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt):
    if not dt:
        return "—"
    return _as_utc(dt).astimezone(PST).strftime("%b %d, %I:%M %p")


def _ago(dt):
    if not dt:
        return "never"
    secs = (datetime.now(timezone.utc) - _as_utc(dt)).total_seconds()
    if secs < 0:
        return "future"
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{int(secs / 86400)}d ago"


def _pst_day_start_utc_naive():
    """Naive-UTC datetime of today's PST midnight — the admin-readability day
    bucket used across these pages (matches the main dashboard's PST framing)."""
    midnight = datetime.now(PST).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


# ─── introspection helpers ─────────────────────────────────────────────────

# Names that mean "when did this happen", in preference order, for the
# "latest activity" probe. Falls back to any DateTime column.
_TS_PREFERENCE = (
    "created_at", "decided_at", "ran_at", "occurred_at", "occurred_on",
    "received_at", "logged_at", "eaten_at", "weighed_at", "date",
)


def _activity_column(table):
    dt_cols = {c.name: c for c in table.columns if isinstance(c.type, DateTime)}
    for name in _TS_PREFERENCE:
        if name in dt_cols:
            return dt_cols[name]
    return next(iter(dt_cols.values()), None)


_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "SID", "PASSWORD", "AUTH", "URL")


def _collect_config():
    """Introspect the config module: booleans become the feature-flag board,
    everything else the settings table. Secrets are shown set/empty only."""
    flags, settings = [], []
    for name in sorted(dir(config)):
        if not name.isupper() or name.startswith("_"):
            continue
        val = getattr(config, name)
        if callable(val) or isinstance(val, type):
            continue
        if any(m in name for m in _SECRET_MARKERS):
            settings.append({"name": name, "value": "••• set" if val else "(empty)", "secret": True})
        elif isinstance(val, bool):
            flags.append({"name": name, "on": val})
        else:
            if isinstance(val, (dict, list, tuple)):
                shown = json.dumps(val, default=str)
            else:
                shown = str(val)
            if len(shown) > 120:
                shown = shown[:120] + "…"
            settings.append({"name": name, "value": shown, "secret": False})
    return flags, settings


def _cell(val, limit=240):
    """Render any column value for the generic browser."""
    if val is None:
        return "·"
    if isinstance(val, datetime):
        return _fmt(val)
    if isinstance(val, bool):
        return "✓" if val else "✗"
    if isinstance(val, (dict, list)):
        val = json.dumps(val, default=str)
    s = str(val)
    return s[:limit] + "…" if len(s) > limit else s


# ─── shared page shell ─────────────────────────────────────────────────────

_SHELL = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} — Cued Admin</title>
<style>
:root{--bg:#050506;--bg2:#0A0A0C;--surface:#111114;--card:#19191D;--border:#1F1F24;
--text:#F5F5F7;--text2:#A1A1A6;--text3:#6E6E73;--accent:#7C6EFF;--green:#30D158;
--yellow:#FFD60A;--red:#FF453A;--blue:#0A84FF}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}a:hover{opacity:.8}
.topbar{display:flex;gap:18px;align-items:center;padding:14px 28px;border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:10}
.topbar .wordmark{font-weight:700;font-size:15px;letter-spacing:-.4px;color:var(--text)}
.topbar a.nav{font-size:13px;color:var(--text2)}
.topbar a.nav.here{color:var(--accent);font-weight:600}
.content{padding:28px;max-width:1280px}
h1{font-size:20px;font-weight:700;letter-spacing:-.4px}
.sub{color:var(--text3);font-size:13px;margin:4px 0 24px}
.section{margin-bottom:32px}
.section-title{font-size:13px;font-weight:600;color:var(--text2);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.grid{display:grid;gap:12px;margin-bottom:20px}
.grid-4{grid-template-columns:repeat(4,1fr)}.grid-3{grid-template-columns:repeat(3,1fr)}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px}
.stat-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px}
.stat-val{font-size:26px;font-weight:700;letter-spacing:-1px;line-height:1}
.stat-sub{font-size:11px;color:var(--text3);margin-top:5px}
.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}.blue{color:var(--blue)}.accent{color:var(--accent)}
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:1px;padding:9px 14px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px 14px;border-bottom:1px solid var(--border);color:var(--text2);vertical-align:top}
tr:last-child td{border-bottom:none}
tr.clickable{cursor:pointer}tr.clickable:hover td{background:rgba(124,110,255,.05)}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;white-space:nowrap}
.badge-green{background:rgba(48,209,88,.12);color:var(--green)}
.badge-yellow{background:rgba(255,214,10,.12);color:var(--yellow)}
.badge-red{background:rgba(255,69,58,.12);color:var(--red)}
.badge-gray{background:rgba(110,110,115,.12);color:var(--text3)}
.badge-blue{background:rgba(10,132,255,.12);color:var(--blue)}
.badge-accent{background:rgba(124,110,255,.12);color:var(--accent)}
.empty{color:var(--text3);font-size:13px;padding:28px;text-align:center}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.prewrap{white-space:pre-wrap;word-break:break-word}
.flag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:8px}
.flag{display:flex;justify-content:space-between;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:9px 14px;font-size:12px}
.flag .fname{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--text2)}
.info-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin-bottom:12px}
.info-card h3{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px}
.btn{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 14px;font-size:12px;font-weight:600;cursor:pointer;transition:border-color .15s}
.btn:hover{border-color:var(--accent)}
.btn:disabled{opacity:.5;cursor:default}
.btn.warn{color:var(--red);border-color:rgba(255,69,58,.4)}
.btn-mini{padding:2px 8px;font-size:10px;border-radius:5px;font-weight:500}
.notice{background:rgba(255,69,58,.08);border:1px solid rgba(255,69,58,.3);border-radius:8px;padding:12px 16px;font-size:12.5px;color:var(--red);margin-bottom:20px}
</style>
</head>
<body>
<div class="topbar">
  <span class="wordmark">cued <span style="color:var(--text3);font-weight:400">/ debug</span></span>
  <a class="nav {{ 'here' if active_nav == 'dashboard' }}" href="/admin">Dashboard</a>
  <a class="nav {{ 'here' if active_nav == 'system' }}" href="/admin/system">System</a>
  <a class="nav {{ 'here' if active_nav == 'heartbeat' }}" href="/admin/heartbeat">Heartbeat</a>
  <a class="nav {{ 'here' if active_nav == 'consolidation' }}" href="/admin/consolidation">Memory Jobs</a>
  <span style="margin-left:auto;font-size:11px;color:var(--text3)">{{ rendered_at }}</span>
</div>
<div class="content">
<h1>{{ title }}</h1>
<p class="sub">{{ subtitle }}</p>
__BODY__
</div>
</body>
</html>
"""


def _render(active_nav, title, subtitle, body_tpl, **ctx):
    return render_template_string(
        _SHELL.replace("__BODY__", body_tpl),
        active_nav=active_nav,
        title=title,
        subtitle=subtitle,
        rendered_at=datetime.now(PST).strftime("%b %d, %I:%M:%S %p PST"),
        **ctx,
    )


# ─── /admin/system ─────────────────────────────────────────────────────────

_SYSTEM_BODY = """
{% if not auth_on %}
<div class="notice">⚠ ADMIN_PASSWORD is not set — the entire console, including its write
actions, is UNAUTHENTICATED. Set the ADMIN_PASSWORD env var (Railway → Variables) to
enable HTTP Basic auth on every /admin route.</div>
{% endif %}
<div class="section">
  <div class="section-title">Background Job Health</div>
  <div class="table-wrap">
    <table>
      <tr><th>System</th><th>Enabled</th><th>Last Activity</th><th>Freshness</th><th>Today</th><th>Notes</th></tr>
      {% for j in jobs %}
      <tr>
        <td style="color:var(--text);font-weight:500">{{ j.name }}</td>
        <td>{% if j.enabled is none %}<span class="badge badge-gray">ALWAYS</span>
            {% elif j.enabled %}<span class="badge badge-green">ON</span>
            {% else %}<span class="badge badge-gray">OFF</span>{% endif %}</td>
        <td>{{ j.last }} <span style="color:var(--text3)">({{ j.ago }})</span></td>
        <td><span class="badge badge-{{ j.health_color }}">{{ j.health }}</span></td>
        <td>{{ j.today }}</td>
        <td style="color:var(--text3)">{{ j.notes }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Feature Flags — introspected live from config (new flags appear automatically)</div>
  <div class="flag-grid">
    {% for f in flags %}
    <div class="flag">
      <span class="fname">{{ f.name }}</span>
      {% if f.on %}<span class="badge badge-green">ON</span>{% else %}<span class="badge badge-gray">OFF</span>{% endif %}
    </div>
    {% endfor %}
  </div>
</div>

<div class="section">
  <div class="section-title">Settings &amp; Knobs</div>
  <div class="table-wrap">
    <table>
      <tr><th>Name</th><th>Value</th></tr>
      {% for s in settings %}
      <tr><td class="mono">{{ s.name }}</td><td class="mono" style="color:{{ 'var(--text3)' if s.secret else 'var(--text2)' }}">{{ s.value }}</td></tr>
      {% endfor %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Database — introspected from models (new tables appear automatically)</div>
  <div class="table-wrap">
    <table>
      <tr><th>Table</th><th>Rows</th><th>Columns</th><th>Latest Activity</th><th></th></tr>
      {% for t in tables %}
      <tr class="clickable" onclick="window.location.href='/admin/data/{{ t.name }}'">
        <td style="color:var(--text);font-weight:500" class="mono">{{ t.name }}</td>
        <td>{{ t.rows }}</td>
        <td>{{ t.cols }}</td>
        <td>{{ t.latest }} <span style="color:var(--text3)">{{ t.ago }}</span></td>
        <td><a href="/admin/data/{{ t.name }}">browse →</a></td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
"""


def _job_health(session, now_utc_naive):
    """The one hand-written layer: what freshness MEANS per background system.
    Each row still reads its enable-flag and cadence from config at request
    time, so retuning intervals or flipping flags updates the health logic."""
    day_start = _pst_day_start_utc_naive()

    def stale_check(last, max_age_min, enabled):
        if enabled is False:
            return "DISABLED", "gray"
        if not last:
            return "NO DATA", "yellow" if enabled else "gray"
        age_min = (now_utc_naive - last).total_seconds() / 60
        if age_min <= max_age_min:
            return "HEALTHY", "green"
        return "STALE", "red"

    jobs = []

    last_tick = session.query(func.max(HeartbeatTick.decided_at)).scalar()
    ticks_today = session.query(HeartbeatTick).filter(HeartbeatTick.decided_at >= day_start).count()
    spoke_today = session.query(HeartbeatTick).filter(
        HeartbeatTick.decided_at >= day_start, HeartbeatTick.spoke == True).count()  # noqa: E712
    h, c = stale_check(last_tick, config.HEARTBEAT_TICK_MINUTES * 2 + config.HEARTBEAT_JITTER_SECONDS / 60,
                       config.HEARTBEAT_ENABLED)
    jobs.append({"name": "Heartbeat", "enabled": config.HEARTBEAT_ENABLED,
                 "last": _fmt(last_tick), "ago": _ago(last_tick), "health": h, "health_color": c,
                 "today": f"{ticks_today} ticks / {spoke_today} spoke",
                 "notes": f"every {config.HEARTBEAT_TICK_MINUTES}m, cap {config.HEARTBEAT_MAX_PER_DAY}/day"})

    last_run = session.query(func.max(ConsolidationRun.ran_at)).scalar()
    aborted_7d = session.query(ConsolidationRun).filter(
        ConsolidationRun.ran_at >= now_utc_naive - timedelta(days=7),
        ConsolidationRun.aborted == True).count()  # noqa: E712
    h, c = stale_check(last_run, 26 * 60, config.CONSOLIDATION_ENABLED)
    jobs.append({"name": "Nightly Consolidation", "enabled": config.CONSOLIDATION_ENABLED,
                 "last": _fmt(last_run), "ago": _ago(last_run), "health": h, "health_color": c,
                 "today": f"{aborted_7d} aborts (7d)",
                 "notes": f"runs at {config.CONSOLIDATION_HOUR}:00 PT; abort cap {config.CONSOLIDATION_MAX_DELTA_FRACTION}"})

    last_dig = session.query(func.max(EpisodicDigest.created_at)).scalar()
    dig_7d = session.query(EpisodicDigest).filter(
        EpisodicDigest.created_at >= now_utc_naive - timedelta(days=7)).count()
    # Digests fire only when a conversation goes quiet, so freshness is loose:
    # flag stale only past ~3 days with the flag on.
    h, c = stale_check(last_dig, 3 * 24 * 60, config.EPISODIC_ENABLED)
    jobs.append({"name": "Episodic Digest", "enabled": config.EPISODIC_ENABLED,
                 "last": _fmt(last_dig), "ago": _ago(last_dig), "health": h, "health_color": c,
                 "today": f"{dig_7d} digests (7d)",
                 "notes": f"sweep {config.EPISODIC_SWEEP_MINUTES}m, quiet {config.EPISODIC_QUIET_MINUTES}m"})

    last_ev = session.query(func.max(Event.created_at)).scalar()
    ev_today = session.query(Event).filter(Event.created_at >= day_start).count()
    jobs.append({"name": "Event Detection", "enabled": None,
                 "last": _fmt(last_ev), "ago": _ago(last_ev), "health": "PASSIVE", "health_color": "gray",
                 "today": f"{ev_today} events today",
                 "notes": "regex floor + log_event tool; fires only on matching inbound"})

    last_tok = session.query(func.max(TokenUsage.created_at)).scalar()
    cost_today = session.query(func.sum(TokenUsage.cost_usd)).filter(
        TokenUsage.created_at >= day_start).scalar() or 0.0
    calls_today = session.query(TokenUsage).filter(TokenUsage.created_at >= day_start).count()
    jobs.append({"name": "API Calls (token_usage)", "enabled": None,
                 "last": _fmt(last_tok), "ago": _ago(last_tok), "health": "PASSIVE", "health_color": "gray",
                 "today": f"{calls_today} calls / ${cost_today:.2f}",
                 "notes": "every Anthropic call tracks here — silence during active hours = something broke"})

    jobs.append({"name": "Legacy Scheduler", "enabled": config.LEGACY_SCHEDULER_ENABLED,
                 "last": "—", "ago": "", "health": "DISABLED" if not config.LEGACY_SCHEDULER_ENABLED else "ON",
                 "health_color": "gray" if not config.LEGACY_SCHEDULER_ENABLED else "green",
                 "today": "", "notes": "flag-disabled so heartbeat owns proactive (Phase 6 deletes it)"})

    return jobs


@admin_system_bp.route("/admin/system")
def system_page():
    session = get_session()
    try:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        flags, settings = _collect_config()
        jobs = _job_health(session, now_naive)

        tables = []
        for t in Base.metadata.sorted_tables:
            rows = session.execute(select(func.count()).select_from(t)).scalar()
            ts_col = _activity_column(t)
            latest = session.execute(select(func.max(ts_col))).scalar() if ts_col is not None else None
            tables.append({
                "name": t.name, "rows": rows, "cols": len(t.columns),
                "latest": _fmt(latest), "ago": f"({_ago(latest)})" if latest else "",
            })
        tables.sort(key=lambda x: x["name"])

        return _render("system", "System Health",
                       "Flags, background jobs, and schema — all introspected live, nothing hardcoded",
                       _SYSTEM_BODY, jobs=jobs, flags=flags, settings=settings, tables=tables,
                       auth_on=bool(config.ADMIN_PASSWORD))
    finally:
        session.close()


# ─── /admin/data/<table> — generic browser ─────────────────────────────────

_DATA_BODY = """
<div style="display:flex;gap:10px;align-items:center;margin-bottom:14px;font-size:12px;color:var(--text3)">
  <span>{{ total }} rows total — showing newest {{ shown }}</span>
  {% if has_user_id %}
  <form method="get" style="display:flex;gap:6px;align-items:center">
    <input name="user_id" value="{{ user_id_filter or '' }}" placeholder="filter user_id"
      style="background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:5px 9px;font-size:12px;width:110px">
    <button style="background:var(--accent);border:none;border-radius:6px;color:#fff;padding:5px 12px;font-size:12px;cursor:pointer">Filter</button>
    {% if user_id_filter %}<a href="/admin/data/{{ table_name }}">clear</a>{% endif %}
  </form>
  {% endif %}
</div>
<div class="table-wrap">
  <table>
    <tr>{% for c in columns %}<th>{{ c }}</th>{% endfor %}</tr>
    {% for row in rows %}
    <tr>{% for v in row %}<td class="mono prewrap" style="max-width:420px">{{ v }}</td>{% endfor %}</tr>
    {% endfor %}
    {% if not rows %}<tr><td colspan="{{ columns|length }}" class="empty">No rows.</td></tr>{% endif %}
  </table>
</div>
"""


@admin_system_bp.route("/admin/data/<table_name>")
def data_browser(table_name):
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return f"Unknown table: {table_name}", 404
    session = get_session()
    try:
        columns = list(table.columns)
        stmt = select(table)
        count_stmt = select(func.count()).select_from(table)

        has_user_id = "user_id" in table.columns
        user_id_filter = request.args.get("user_id", type=int)
        if has_user_id and user_id_filter:
            stmt = stmt.where(table.c.user_id == user_id_filter)
            count_stmt = count_stmt.where(table.c.user_id == user_id_filter)

        # Newest first: primary key desc when there is one, else activity column.
        pk = list(table.primary_key.columns)
        order_col = pk[0] if pk else _activity_column(table)
        if order_col is not None:
            stmt = stmt.order_by(order_col.desc())

        limit = min(request.args.get("limit", 100, type=int), 500)
        total = session.execute(count_stmt).scalar()
        result = session.execute(stmt.limit(limit)).all()
        rows = [[_cell(v) for v in row] for row in result]

        return _render("system", f"Table: {table_name}",
                       "Generic browser — columns come straight from the model definition",
                       _DATA_BODY, table_name=table_name, columns=[c.name for c in columns],
                       rows=rows, total=total, shown=len(rows),
                       has_user_id=has_user_id, user_id_filter=user_id_filter)
    finally:
        session.close()


# ─── /admin/heartbeat ──────────────────────────────────────────────────────

_HEARTBEAT_BODY = """
<div class="grid grid-4">
  <div class="stat-card">
    <div class="stat-label">Ticks Today</div>
    <div class="stat-val">{{ ticks_today }}</div>
    <div class="stat-sub">decisions made (PST day)</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Spoke Today</div>
    <div class="stat-val accent">{{ spoke_today }}</div>
    <div class="stat-sub">cap {{ max_per_day }}/user/day</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Speak Rate (7d)</div>
    <div class="stat-val {{ 'green' if speak_rate_7d <= 25 else 'yellow' if speak_rate_7d <= 50 else 'red' }}">{{ speak_rate_7d }}%</div>
    <div class="stat-sub">{{ spoke_7d }} of {{ ticks_7d }} ticks — silent is the default</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Searches Today</div>
    <div class="stat-val blue">{{ searches_today }}</div>
    <div class="stat-sub">budget {{ search_budget }}/user/day</div>
  </div>
</div>

{% if per_user %}
<div class="section">
  <div class="section-title">Per-User Today</div>
  <div class="table-wrap">
    <table>
      <tr><th>User</th><th>Ticks</th><th>Spoke</th><th>Daily Cap</th><th>Searches</th><th>Search Budget</th><th>Last Decision</th></tr>
      {% for u in per_user %}
      <tr class="clickable" onclick="window.location.href='/admin/user/{{ u.user_id }}/debug'">
        <td style="color:var(--accent);font-weight:500">{{ u.name }}</td>
        <td>{{ u.ticks }}</td>
        <td>{{ u.spoke }}</td>
        <td><span class="badge badge-{{ 'red' if u.spoke >= max_per_day else 'green' }}">{{ u.spoke }}/{{ max_per_day }}</span></td>
        <td>{{ u.searches }}</td>
        <td><span class="badge badge-{{ 'red' if u.searches >= search_budget else 'blue' }}">{{ u.searches }}/{{ search_budget }}</span></td>
        <td style="color:var(--text3)">{{ u.last }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>
{% endif %}

<div class="section">
  <div class="section-title">Recent Tick Decisions (newest {{ ticks|length }})</div>
  <div class="table-wrap">
    <table>
      <tr><th>When</th><th>User</th><th>Decision</th><th>Reason / Message</th><th>Search</th></tr>
      {% for t in ticks %}
      <tr class="clickable" onclick="window.location.href='/admin/user/{{ t.user_id }}/debug'">
        <td style="white-space:nowrap;color:var(--text3)">{{ t.when }}</td>
        <td style="color:var(--accent)">{{ t.name }}</td>
        <td>{% if t.spoke %}<span class="badge badge-accent">SPOKE</span>{% else %}<span class="badge badge-gray">SILENT</span>{% endif %}</td>
        <td class="prewrap" style="max-width:560px">{% if t.spoke and t.message %}<span style="color:var(--text)">{{ t.message }}</span>{% if t.reason and t.reason != 'spoke' %}<br><span style="color:var(--text3);font-size:11px">{{ t.reason }}</span>{% endif %}{% else %}{{ t.reason }}{% endif %}</td>
        <td>{% if t.search_used %}<span class="badge badge-blue">USED</span> <span class="mono" style="font-size:10.5px">{{ t.search_query or '' }}</span>
            {% elif t.search_available %}<span class="badge badge-gray">OFFERED</span>
            {% else %}·{% endif %}</td>
      </tr>
      {% endfor %}
      {% if not ticks %}<tr><td colspan="5" class="empty">No heartbeat ticks recorded yet.</td></tr>{% endif %}
    </table>
  </div>
</div>
"""


@admin_system_bp.route("/admin/heartbeat")
def heartbeat_page():
    session = get_session()
    try:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        day_start = _pst_day_start_utc_naive()
        week_start = now_naive - timedelta(days=7)

        today_ticks = session.query(HeartbeatTick).filter(HeartbeatTick.decided_at >= day_start).all()
        ticks_7d = session.query(HeartbeatTick).filter(HeartbeatTick.decided_at >= week_start).count()
        spoke_7d = session.query(HeartbeatTick).filter(
            HeartbeatTick.decided_at >= week_start, HeartbeatTick.spoke == True).count()  # noqa: E712

        user_map = {u.id: u.name for u in session.query(User).all()}

        per_user = {}
        for t in today_ticks:
            s = per_user.setdefault(t.user_id, {"user_id": t.user_id,
                                                "name": user_map.get(t.user_id, f"#{t.user_id}"),
                                                "ticks": 0, "spoke": 0, "searches": 0, "last_dt": None})
            s["ticks"] += 1
            s["spoke"] += 1 if t.spoke else 0
            s["searches"] += 1 if t.search_used else 0
            if s["last_dt"] is None or t.decided_at > s["last_dt"]:
                s["last_dt"] = t.decided_at
        per_user_rows = sorted(per_user.values(), key=lambda s: -s["ticks"])
        for s in per_user_rows:
            s["last"] = _fmt(s.pop("last_dt"))

        recent = (session.query(HeartbeatTick)
                  .order_by(HeartbeatTick.decided_at.desc()).limit(150).all())
        ticks_data = [{
            "when": _fmt(t.decided_at),
            "user_id": t.user_id,
            "name": user_map.get(t.user_id, f"#{t.user_id}"),
            "spoke": t.spoke,
            "reason": (t.reason or "")[:600],
            "message": (t.message or "")[:600],
            "search_available": t.search_available,
            "search_used": t.search_used,
            "search_query": (t.search_query or "")[:120],
        } for t in recent]

        return _render("heartbeat", "Heartbeat",
                       "Every tick decision — spoke or stayed silent, and why. The anti-repetition trail.",
                       _HEARTBEAT_BODY,
                       ticks_today=len(today_ticks),
                       spoke_today=sum(1 for t in today_ticks if t.spoke),
                       searches_today=sum(1 for t in today_ticks if t.search_used),
                       ticks_7d=ticks_7d, spoke_7d=spoke_7d,
                       speak_rate_7d=round(spoke_7d / ticks_7d * 100) if ticks_7d else 0,
                       max_per_day=config.HEARTBEAT_MAX_PER_DAY,
                       search_budget=config.HEARTBEAT_SEARCH_MAX_PER_DAY,
                       per_user=per_user_rows, ticks=ticks_data)
    finally:
        session.close()


# ─── /admin/consolidation ──────────────────────────────────────────────────

_CONSOLIDATION_BODY = """
<div class="grid grid-4">
  <div class="stat-card">
    <div class="stat-label">Runs (7d)</div>
    <div class="stat-val">{{ runs_7d }}</div>
    <div class="stat-sub">nightly consolidation passes</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Aborted (7d)</div>
    <div class="stat-val {{ 'red' if aborted_7d > 0 else 'green' }}">{{ aborted_7d }}</div>
    <div class="stat-sub">bounded-delta guardrail trips</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Entries Closed (7d)</div>
    <div class="stat-val">{{ removed_7d }}</div>
    <div class="stat-sub">merged / expired / superseded</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Episodic Digests (7d)</div>
    <div class="stat-val blue">{{ digests_7d }}</div>
    <div class="stat-sub">life-context notes captured</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Consolidation Runs — the one-line summary is the daily sanity check</div>
  <div class="table-wrap">
    <table>
      <tr><th>Ran At</th><th>User</th><th>Valid Before</th><th>Removed</th><th>Status</th><th>Summary</th><th></th></tr>
      {% for r in runs %}
      <tr class="clickable" onclick="window.location.href='/admin/user/{{ r.user_id }}/debug'">
        <td style="white-space:nowrap;color:var(--text3)">{{ r.when }}</td>
        <td style="color:var(--accent)">{{ r.name }}</td>
        <td>{{ r.valid_before }}</td>
        <td>{{ r.removed }}</td>
        <td>{% if r.aborted %}<span class="badge badge-red">ABORTED</span>{% else %}<span class="badge badge-green">OK</span>{% endif %}</td>
        <td class="prewrap" style="max-width:600px">{{ r.summary }}</td>
        <td onclick="event.stopPropagation()">{% if r.can_rollback %}<button class="btn btn-mini warn" onclick="rollbackRun({{ r.user_id }}, {{ r.id }})">rollback…</button>{% endif %}</td>
      </tr>
      {% endfor %}
      {% if not runs %}<tr><td colspan="7" class="empty">No consolidation runs recorded yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Recent Episodic Digests</div>
  <div class="table-wrap">
    <table>
      <tr><th>Occurred</th><th>User</th><th>Digest</th></tr>
      {% for d in digests %}
      <tr class="clickable" onclick="window.location.href='/admin/user/{{ d.user_id }}/debug'">
        <td style="white-space:nowrap;color:var(--text3)">{{ d.when }}</td>
        <td style="color:var(--accent)">{{ d.name }}</td>
        <td class="prewrap" style="max-width:700px">{{ d.text }}</td>
      </tr>
      {% endfor %}
      {% if not digests %}<tr><td colspan="3" class="empty">No episodic digests yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<script>
async function rollbackRun(uid, runId) {
  if (!confirm('Restore user #' + uid + ' profile memory to the PRE-RUN snapshot of run #' + runId + '? This overwrites their current profile memory.')) return;
  const r = await fetch('/admin/user/' + uid + '/consolidation/' + runId + '/rollback',
                        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
  const d = await r.json();
  alert(d.result || d.message); location.reload();
}
</script>
"""


@admin_system_bp.route("/admin/consolidation")
def consolidation_page():
    session = get_session()
    try:
        week_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        user_map = {u.id: u.name for u in session.query(User).all()}

        week_runs = session.query(ConsolidationRun).filter(ConsolidationRun.ran_at >= week_start).all()
        runs = (session.query(ConsolidationRun)
                .order_by(ConsolidationRun.ran_at.desc()).limit(50).all())
        runs_data = [{
            "id": r.id, "when": _fmt(r.ran_at), "user_id": r.user_id,
            "name": user_map.get(r.user_id, f"#{r.user_id}"),
            "valid_before": r.valid_before, "removed": r.removed_count,
            "aborted": r.aborted, "can_rollback": r.prev_profile is not None,
            "summary": (r.summary or "—")[:800],
        } for r in runs]

        digests_7d = session.query(EpisodicDigest).filter(
            EpisodicDigest.created_at >= week_start).count()
        digests = (session.query(EpisodicDigest).filter(EpisodicDigest.deleted_at.is_(None))
                   .order_by(EpisodicDigest.occurred_on.desc()).limit(40).all())
        digests_data = [{
            "when": _fmt(d.occurred_on), "user_id": d.user_id,
            "name": user_map.get(d.user_id, f"#{d.user_id}"),
            "text": (d.text or "")[:900],
        } for d in digests]

        return _render("consolidation", "Memory Jobs",
                       "Nightly consolidation audit trail + episodic digests. An ABORT means the delta guardrail refused a run.",
                       _CONSOLIDATION_BODY,
                       runs_7d=len(week_runs),
                       aborted_7d=sum(1 for r in week_runs if r.aborted),
                       removed_7d=sum(r.removed_count or 0 for r in week_runs if not r.aborted),
                       digests_7d=digests_7d,
                       runs=runs_data, digests=digests_data)
    finally:
        session.close()


# ─── /admin/user/<id>/debug ────────────────────────────────────────────────

_USER_DEBUG_BODY = """
<div style="margin:-14px 0 20px;font-size:13px">
  <a href="/admin/user/{{ u.id }}">← standard user page</a>
</div>

<div class="section">
  <div class="section-title">Actions — manual job runs &amp; audited corrections</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
    <button class="btn" onclick="runJob(this,'heartbeat-dry-run')" title="Runs the real decide() call. 1 API call; nothing sent, no tick row.">Heartbeat: dry run</button>
    <button class="btn warn" onclick="if(confirm('LIVE heartbeat tick for {{ u.name }}. Guardrails apply, but if the model decides to speak this SENDS A REAL SMS. Continue?'))runJob(this,'heartbeat-tick')">Heartbeat: LIVE tick</button>
    <button class="btn" onclick="if(confirm('Run consolidation for {{ u.name }} now? Mutates profile memory (audited, snapshot + rollback below).'))runJob(this,'consolidate')">Consolidate now</button>
    <button class="btn" onclick="runJob(this,'episodic-digest')" title="Respects the normal quiet/min-message gates; advances the watermark.">Episodic digest</button>
    <button class="btn" onclick="runJob(this,'recompute-totals')" title="Rebuild today's macro totals from active meals.">Recompute totals</button>
  </div>
  <pre id="job-result" class="mono prewrap" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:11.5px;color:var(--text2);margin:0">Job output appears here.</pre>
</div>

<div class="section">
  <div class="section-title">Live Coach State</div>
  <div class="grid grid-3">
    <div class="info-card">
      <h3>Session</h3>
      <table style="font-size:12.5px">
        <tr><td style="color:var(--text3)">session_state</td><td class="mono prewrap">{{ state.session_state }}</td></tr>
        <tr><td style="color:var(--text3)">quiet_until</td><td>{{ state.quiet_until }}</td></tr>
        <tr><td style="color:var(--text3)">unanswered_count</td><td>{{ u.unanswered_count }}</td></tr>
        <tr><td style="color:var(--text3)">onboarding_step</td><td>{{ u.onboarding_step }}</td></tr>
        <tr><td style="color:var(--text3)">timezone</td><td>{{ u.user_timezone }}</td></tr>
      </table>
    </div>
    <div class="info-card">
      <h3>Split Pointer</h3>
      <table style="font-size:12.5px">
        <tr><td style="color:var(--text3)">last completed day</td><td>{{ u.split_pointer_day or '—' }}</td></tr>
        <tr><td style="color:var(--text3)">at</td><td>{{ state.split_pointer_at }}</td></tr>
        <tr><td style="color:var(--text3)">source</td><td>{{ u.split_pointer_source or '—' }}</td></tr>
        <tr><td style="color:var(--text3)">current_split</td><td>{{ u.current_split or '—' }}</td></tr>
        <tr><td style="color:var(--text3)">confirmed days</td><td>{{ u.confirmed_training_days or '—' }}</td></tr>
      </table>
    </div>
    <div class="info-card">
      <h3>Watermarks &amp; Targets</h3>
      <table style="font-size:12.5px">
        <tr><td style="color:var(--text3)">summary watermark</td><td>msg #{{ u.last_compressed_message_id or '—' }}</td></tr>
        <tr><td style="color:var(--text3)">episodic watermark</td><td>msg #{{ u.last_episodic_message_id or '—' }}</td></tr>
        <tr><td style="color:var(--text3)">calorie / protein target</td><td>{{ u.calorie_target or '—' }} / {{ u.protein_target or '—' }}g</td></tr>
        <tr><td style="color:var(--text3)">today's totals ({{ u.totals_date or '—' }})</td><td>{{ u.calories_today }} cal / {{ u.protein_today }}p / {{ u.carbs_today }}c / {{ u.fat_today }}f</td></tr>
        <tr><td style="color:var(--text3)">active_meal_id</td><td>{{ u.active_meal_id or '—' }}</td></tr>
      </table>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-title">Profile Memory (user_profile_memory) — {{ memory_chars }} chars of {{ memory_cap }} cap</div>
  {% if memory_categories %}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px">
    {% for cat in memory_categories %}
    <div class="info-card" style="margin-bottom:0">
      <h3>{{ cat.name }} <span style="color:var(--text3);text-transform:none;letter-spacing:0">— {{ cat.entries|length }} entries, {{ cat.chars }} chars</span></h3>
      {% for e in cat.entries %}
      <div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12.5px">
        <div class="prewrap" style="color:{{ 'var(--text3)' if e.closed else 'var(--text)' }}">{{ e.text }}</div>
        <div style="font-size:10.5px;color:var(--text3);margin-top:2px;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <span>{{ e.meta }}</span>
          {% if e.safety %}<span class="badge badge-red" style="font-size:9px">SAFETY</span>{% endif %}
          {% if e.closed %}<span class="badge badge-gray" style="font-size:9px">CLOSED</span>{% endif %}
          {% if e.closable %}<button class="btn btn-mini" onclick="closeMemory('{{ e.id }}')">close…</button>{% endif %}
        </div>
      </div>
      {% endfor %}
      {% if not cat.entries %}<div class="empty" style="padding:8px">empty</div>{% endif %}
    </div>
    {% endfor %}
  </div>
  {% else %}<p class="empty">No structured profile memory yet (legacy blob below).</p>{% endif %}
</div>

{% if u.memory %}
<div class="section">
  <div class="section-title">Legacy Memory Blob</div>
  <div class="info-card"><div class="prewrap" style="font-size:12.5px;color:var(--text2)">{{ u.memory }}</div></div>
</div>
{% endif %}

{% if u.coaching_summary %}
<div class="section">
  <div class="section-title">Coaching Summary (rolling)</div>
  <div class="info-card"><div class="prewrap" style="font-size:12.5px;color:var(--text2)">{{ u.coaching_summary }}</div></div>
</div>
{% endif %}

{% if u.delivered_coaching_points %}
<div class="section">
  <div class="section-title">Delivered Coaching Points (anti-repetition)</div>
  <div class="info-card"><div class="prewrap" style="font-size:12.5px;color:var(--text2)">{{ u.delivered_coaching_points }}</div></div>
</div>
{% endif %}

<div class="section">
  <div class="section-title">Episodic Digests ({{ digests|length }})</div>
  <div class="table-wrap">
    <table>
      <tr><th>Occurred</th><th>Digest</th></tr>
      {% for d in digests %}<tr><td style="white-space:nowrap;color:var(--text3)">{{ d.when }}</td><td class="prewrap">{{ d.text }}</td></tr>{% endfor %}
      {% if not digests %}<tr><td colspan="2" class="empty">None yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Recent Meals — corrections go through the audited manage_log path</div>
  <div class="table-wrap">
    <table>
      <tr><th>ID</th><th>Eaten</th><th>Description</th><th>Cal</th><th>P / C / F</th><th>Source</th><th></th></tr>
      {% for m in meals %}
      <tr><td class="mono">{{ m.id }}</td>
          <td style="white-space:nowrap;color:var(--text3)">{{ m.when }}</td>
          <td class="prewrap" style="max-width:380px">{{ m.description }}</td>
          <td>{{ m.calories }}</td>
          <td>{{ m.protein_g }} / {{ m.carbs_g }} / {{ m.fat_g }}</td>
          <td>{{ m.source }}</td>
          <td style="white-space:nowrap">
            <button class="btn btn-mini" data-entity="meal" data-id="{{ m.id }}" data-current="{{ m.edit_json }}" onclick="editRow(this)">edit…</button>
            <button class="btn btn-mini warn" onclick="delRow('meal', {{ m.id }})">delete</button>
          </td></tr>
      {% endfor %}
      {% if not meals %}<tr><td colspan="7" class="empty">No active meals.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Recent Workouts</div>
  <div class="table-wrap">
    <table>
      <tr><th>ID</th><th>Date</th><th>Type</th><th>User Notes</th><th></th></tr>
      {% for w in workouts %}
      <tr><td class="mono">{{ w.id }}</td>
          <td style="white-space:nowrap;color:var(--text3)">{{ w.when }}</td>
          <td>{{ w.type }}</td>
          <td class="prewrap" style="max-width:420px">{{ w.notes }}</td>
          <td style="white-space:nowrap">
            <button class="btn btn-mini" data-entity="workout" data-id="{{ w.id }}" data-current="{{ w.edit_json }}" onclick="editRow(this)">edit…</button>
            <button class="btn btn-mini warn" onclick="delRow('workout', {{ w.id }})">delete</button>
          </td></tr>
      {% endfor %}
      {% if not workouts %}<tr><td colspan="5" class="empty">No active workouts.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Events (dated, auto-expiring — last {{ events|length }})</div>
  <div class="table-wrap">
    <table>
      <tr><th>ID</th><th>Occurred</th><th>Type</th><th>Ends</th><th>Source</th><th>Trigger Text</th><th></th></tr>
      {% for e in events %}
      <tr><td class="mono">{{ e.id }}</td>
          <td style="white-space:nowrap;color:var(--text3)">{{ e.when }}</td>
          <td><span class="badge badge-accent">{{ e.type }}</span></td>
          <td style="color:var(--text3)">{{ e.ends }}</td>
          <td>{{ e.source }}</td>
          <td class="prewrap" style="max-width:420px">{{ e.raw }}</td>
          <td><button class="btn btn-mini warn" onclick="delRow('event', {{ e.id }})">delete</button></td></tr>
      {% endfor %}
      {% if not events %}<tr><td colspan="7" class="empty">None yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Heartbeat Ticks (last {{ ticks|length }})</div>
  <div class="table-wrap">
    <table>
      <tr><th>When</th><th>Decision</th><th>Reason / Message</th><th>Search</th></tr>
      {% for t in ticks %}
      <tr><td style="white-space:nowrap;color:var(--text3)">{{ t.when }}</td>
          <td>{% if t.spoke %}<span class="badge badge-accent">SPOKE</span>{% else %}<span class="badge badge-gray">SILENT</span>{% endif %}</td>
          <td class="prewrap" style="max-width:560px">{% if t.spoke and t.message %}<span style="color:var(--text)">{{ t.message }}</span>{% else %}{{ t.reason }}{% endif %}</td>
          <td>{% if t.search_used %}<span class="badge badge-blue">USED</span>{% elif t.search_available %}<span class="badge badge-gray">OFFERED</span>{% else %}·{% endif %}</td></tr>
      {% endfor %}
      {% if not ticks %}<tr><td colspan="4" class="empty">None yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">Consolidation Runs (last {{ runs|length }})</div>
  <div class="table-wrap">
    <table>
      <tr><th>Ran At</th><th>Valid Before</th><th>Removed</th><th>Status</th><th>Summary</th><th></th></tr>
      {% for r in runs %}
      <tr><td style="white-space:nowrap;color:var(--text3)">{{ r.when }}</td>
          <td>{{ r.valid_before }}</td><td>{{ r.removed }}</td>
          <td>{% if r.aborted %}<span class="badge badge-red">ABORTED</span>{% else %}<span class="badge badge-green">OK</span>{% endif %}</td>
          <td class="prewrap" style="max-width:600px">{{ r.summary }}</td>
          <td>{% if r.can_rollback %}<button class="btn btn-mini warn" onclick="rollbackRun({{ u.id }}, {{ r.id }})">rollback…</button>{% endif %}</td></tr>
      {% endfor %}
      {% if not runs %}<tr><td colspan="6" class="empty">None yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<div class="section">
  <div class="section-title">API Spend by Call Site (30d)</div>
  <div class="table-wrap">
    <table>
      <tr><th>Site</th><th>Model</th><th>Calls</th><th>Cost</th></tr>
      {% for s in spend %}
      <tr><td class="mono">{{ s.site }}</td><td>{{ s.model }}</td><td>{{ s.calls }}</td><td>${{ '%.4f'|format(s.cost) }}</td></tr>
      {% endfor %}
      {% if not spend %}<tr><td colspan="4" class="empty">No API calls in 30d.</td></tr>{% endif %}
    </table>
  </div>
</div>

<script>
const UID = {{ u.id }};

async function post(url, body) {
  const r = await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {})});
  return r.json();
}

async function runJob(btn, job) {
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = 'Running…';
  const out = document.getElementById('job-result');
  out.textContent = 'Running ' + job + '…';
  try {
    const d = await post('/admin/user/' + UID + '/jobs/' + job);
    out.textContent = JSON.stringify(d, null, 2);
    // Job outcomes land in the feeds below (ticks/runs/digests/totals) — reload
    // shortly so the founder sees the new rows without losing the JSON result.
    if (d.status === 'ok' && job !== 'heartbeat-dry-run') setTimeout(() => location.reload(), 2500);
  } catch (e) {
    out.textContent = 'Request failed: ' + e;
  }
  btn.disabled = false; btn.textContent = orig;
}

async function delRow(entity, id) {
  if (!confirm('Soft-delete ' + entity + ' #' + id + '? (Recoverable in the DB; totals recompute for meals.)')) return;
  const d = await post('/admin/user/' + UID + '/log/' + entity + '/' + id + '/delete');
  alert(d.result || d.message); location.reload();
}

async function editRow(btn) {
  const entity = btn.dataset.entity, id = btn.dataset.id;
  const s = prompt('Edit ' + entity + ' #' + id + ' — JSON of ONLY the fields to change:', btn.dataset.current);
  if (!s) return;
  let fields;
  try { fields = JSON.parse(s); } catch (e) { alert('Invalid JSON: ' + e); return; }
  const d = await post('/admin/user/' + UID + '/log/' + entity + '/' + id + '/edit', {fields});
  alert(d.result || d.message); location.reload();
}

async function closeMemory(entryId) {
  const reason = prompt('Close entry ' + entryId + ' to history.\\nReason (required — recorded as the audit trigger; also what authorizes closing a SAFETY entry):');
  if (!reason || !reason.trim()) return;
  const d = await post('/admin/user/' + UID + '/memory/invalidate', {entry_id: entryId, reason: reason.trim()});
  alert(d.result || d.message); location.reload();
}

async function rollbackRun(uid, runId) {
  if (!confirm('Restore profile memory to the PRE-RUN snapshot of run #' + runId + '? This overwrites the current profile memory.')) return;
  const d = await post('/admin/user/' + uid + '/consolidation/' + runId + '/rollback');
  alert(d.result || d.message); location.reload();
}
</script>
"""


def _memory_categories(profile):
    """Render user_profile_memory generically: every key in the JSON becomes a
    card (including __history__), every field on an entry beyond text/safety is
    shown as metadata — so schema growth in memory.py appears here unedited."""
    if not isinstance(profile, dict):
        return []
    cats = []
    for name, entries in profile.items():
        if not isinstance(entries, list):
            continue
        rendered = []
        for e in entries:
            if not isinstance(e, dict):
                rendered.append({"text": str(e), "meta": "", "safety": False,
                                 "closed": False, "closable": False, "id": None})
                continue
            meta_bits = [f"{k}={v}" for k, v in e.items()
                         if k not in ("text", "safety") and v not in (None, "", [])]
            closed = bool(e.get("invalidated_at")) or name == "__history__"
            rendered.append({
                "text": e.get("text", ""),
                "meta": " · ".join(meta_bits),
                "safety": bool(e.get("safety")),
                "closed": closed,
                "closable": bool(e.get("id")) and not closed,
                "id": e.get("id"),
            })
        chars = sum(len(r["text"]) for r in rendered)
        cats.append({"name": name, "entries": rendered, "chars": chars})
    # Real categories first (order preserved), history last.
    cats.sort(key=lambda c: c["name"] == "__history__")
    return cats


@admin_system_bp.route("/admin/user/<int:user_id>/debug")
def user_debug(user_id):
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return "User not found", 404

        month_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)

        profile = u.user_profile_memory or {}
        memory_categories = _memory_categories(profile)
        memory_chars = sum(c["chars"] for c in memory_categories)

        digests = (session.query(EpisodicDigest)
                   .filter(EpisodicDigest.user_id == user_id, EpisodicDigest.deleted_at.is_(None))
                   .order_by(EpisodicDigest.occurred_on.desc()).limit(25).all())
        events = (session.query(Event)
                  .filter(Event.user_id == user_id, Event.deleted_at.is_(None))
                  .order_by(Event.occurred_at.desc()).limit(30).all())
        meals = active(session, Meal, user_id=user_id).order_by(Meal.eaten_at.desc()).limit(15).all()
        workouts = active(session, Workout, user_id=user_id).order_by(Workout.date.desc()).limit(10).all()
        ticks = (session.query(HeartbeatTick).filter(HeartbeatTick.user_id == user_id)
                 .order_by(HeartbeatTick.decided_at.desc()).limit(30).all())
        runs = (session.query(ConsolidationRun).filter(ConsolidationRun.user_id == user_id)
                .order_by(ConsolidationRun.ran_at.desc()).limit(10).all())
        spend = (session.query(TokenUsage.site, TokenUsage.model,
                               func.count(TokenUsage.id), func.sum(TokenUsage.cost_usd))
                 .filter(TokenUsage.user_id == user_id, TokenUsage.created_at >= month_start)
                 .group_by(TokenUsage.site, TokenUsage.model)
                 .order_by(func.sum(TokenUsage.cost_usd).desc()).all())

        return _render("system", f"Debug: {u.name}",
                       f"User #{u.id} · {u.phone} · full coach-visible state",
                       _USER_DEBUG_BODY,
                       u=u,
                       state={
                           "session_state": json.dumps(u.session_state) if u.session_state else "—",
                           "quiet_until": _fmt(u.quiet_until),
                           "split_pointer_at": _fmt(u.split_pointer_at),
                       },
                       memory_categories=memory_categories,
                       memory_chars=memory_chars,
                       memory_cap=config.USER_PROFILE_MEMORY_CHAR_LIMIT,
                       digests=[{"when": _fmt(d.occurred_on), "text": d.text} for d in digests],
                       events=[{"id": e.id, "when": _fmt(e.occurred_at), "type": e.event_type,
                                "ends": _fmt(e.ends_at) if e.ends_at else "—",
                                "source": e.source, "raw": (e.raw_text or "")[:300]} for e in events],
                       meals=[{"id": m.id, "when": _fmt(m.eaten_at),
                               "description": (m.description or "")[:200],
                               "calories": m.calories or 0, "protein_g": m.protein_g or 0,
                               "carbs_g": m.carbs_g or 0, "fat_g": m.fat_g or 0,
                               "source": m.source or "text",
                               "edit_json": json.dumps({
                                   "description": m.description or "",
                                   "calories": m.calories or 0, "protein_g": m.protein_g or 0,
                                   "carbs_g": m.carbs_g or 0, "fat_g": m.fat_g or 0})}
                              for m in meals],
                       workouts=[{"id": w.id, "when": _fmt(w.date),
                                  "type": w.workout_type or "—",
                                  "notes": (w.user_notes or "")[:300],
                                  "edit_json": json.dumps({
                                      "workout_type": w.workout_type or "",
                                      "notes": w.user_notes or ""})}
                                 for w in workouts],
                       ticks=[{"when": _fmt(t.decided_at), "spoke": t.spoke,
                               "reason": (t.reason or "")[:600], "message": (t.message or "")[:600],
                               "search_available": t.search_available, "search_used": t.search_used}
                              for t in ticks],
                       runs=[{"id": r.id, "when": _fmt(r.ran_at), "valid_before": r.valid_before,
                              "removed": r.removed_count, "aborted": r.aborted,
                              "can_rollback": r.prev_profile is not None,
                              "summary": (r.summary or "—")[:800]} for r in runs],
                       spend=[{"site": s or "?", "model": m or "?", "calls": c, "cost": cost or 0.0}
                              for s, m, c, cost in spend])
    finally:
        session.close()


# ─── Tier 1: operational triggers ──────────────────────────────────────────
# Manual invocation of EXISTING job code paths — no new semantics. Synchronous
# on purpose (Procfile runs the Flask server, no gunicorn worker timeout): the
# founder sees the real result/error in the response instead of tailing logs.

@admin_system_bp.route("/admin/user/<int:user_id>/jobs/<job>", methods=["POST"])
def admin_run_job(user_id, job):
    _audit("job", job=job, user=user_id)
    try:
        if job == "heartbeat-dry-run":
            # decide() WITHOUT send and WITHOUT a tick row: a dry-run must not
            # pollute the tick history the model reads for anti-repetition (a
            # phantom "SPOKE" would suppress a real later send). Costs 1 API call.
            from heartbeat import decide
            spoke, payload, search = decide(user_id)
            return _json({"status": "ok", "job": job, "would_speak": spoke,
                          "message_or_reason": payload, "search": search,
                          "note": "dry run — nothing sent, no tick row written"})

        if job == "heartbeat-tick":
            # The REAL tick: guardrails -> decide -> log, SENDS SMS if it speaks.
            from heartbeat import heartbeat_tick
            heartbeat_tick(user_id)
            session = get_session()
            try:
                t = (session.query(HeartbeatTick).filter_by(user_id=user_id)
                     .order_by(HeartbeatTick.decided_at.desc()).first())
                return _json({"status": "ok", "job": job,
                              "spoke": t.spoke if t else None,
                              "detail": (t.message if t and t.spoke else t.reason) if t else "no tick row (inactive user?)"})
            finally:
                session.close()

        if job == "consolidate":
            from consolidation import consolidate_user
            return _json({"status": "ok", "job": job, "result": consolidate_user(user_id)})

        if job == "episodic-digest":
            # Respects the normal gates (min messages, quiet threshold) and
            # advances the watermark exactly like the scheduled sweep.
            from episodic import digest_user
            return _json({"status": "ok", "job": job, "result": digest_user(user_id)})

        if job == "recompute-totals":
            from models import recompute_daily_totals
            recompute_daily_totals(user_id)
            session = get_session()
            try:
                u = session.get(User, user_id)
                return _json({"status": "ok", "job": job,
                              "totals": {"date": u.totals_date, "calories": u.calories_today,
                                         "protein": u.protein_today, "carbs": u.carbs_today,
                                         "fat": u.fat_today}})
            finally:
                session.close()

        return _json({"status": "error", "message": f"unknown job {job!r}"}, 404)
    except Exception as e:  # noqa: BLE001 — surface the real error to the console
        logger.error("ADMIN_JOB_FAILED job=%s user=%s err=%s", job, user_id, e, exc_info=True)
        return _json({"status": "error", "job": job, "message": f"{type(e).__name__}: {e}"}, 500)


# ─── Tier 2: data corrections (audited paths only) ─────────────────────────

@admin_system_bp.route("/admin/user/<int:user_id>/log/<entity>/<int:rid>/<action>", methods=["POST"])
def admin_manage_log(user_id, entity, rid, action):
    """Edit / soft-delete a meal, workout, or event through handle_manage_log —
    the SAME audited chokepoint the model uses (edits ledger, soft deletes,
    totals recompute), so console corrections are indistinguishable in audit."""
    if action not in ("delete", "edit") or entity not in ("meal", "workout", "event"):
        return _json({"status": "error", "message": "unknown action/entity"}, 404)
    fields = (request.get_json(silent=True) or {}).get("fields") or {}
    _audit("manage_log", user=user_id, entity=entity, id=rid, action=action,
           fields=json.dumps(fields, default=str))
    from agent_tools import handle_manage_log
    result = handle_manage_log(user_id, {"action": action, "entity": entity,
                                         "id": rid, "fields": fields})
    ok = result.startswith("ok")
    return _json({"status": "ok" if ok else "error", "result": result}, 200 if ok else 400)


@admin_system_bp.route("/admin/user/<int:user_id>/memory/invalidate", methods=["POST"])
def admin_memory_invalidate(user_id):
    """Close a profile-memory entry via memory.invalidate_entry — lifecycle
    metadata + move to __history__, never a hard delete. The reason is required
    and recorded as the trigger, which is what lets a SAFETY entry close (the
    trigger-audit guard stays intact; by='admin' marks the human actor)."""
    body = request.get_json(silent=True) or {}
    entry_id = str(body.get("entry_id") or "").strip()
    reason = str(body.get("reason") or "").strip()
    if not entry_id or not reason:
        return _json({"status": "error", "message": "entry_id and reason are both required"}, 400)
    _audit("memory_invalidate", user=user_id, entry=entry_id, reason=reason)
    from memory import invalidate_entry
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return _json({"status": "error", "message": "user not found"}, 404)
        profile = user.user_profile_memory or {}
        if invalidate_entry(profile, entry_id, by="admin", trigger=f"admin: {reason}"):
            user.user_profile_memory = profile
            flag_modified(user, "user_profile_memory")
            session.commit()
            return _json({"status": "ok", "result": f"entry {entry_id} closed to history"})
        return _json({"status": "error",
                      "message": "entry not found (or closure rejected — see logs)"}, 400)
    finally:
        session.close()


@admin_system_bp.route("/admin/user/<int:user_id>/consolidation/<int:run_id>/rollback", methods=["POST"])
def admin_consolidation_rollback(user_id, run_id):
    """Restore profile memory to a run's pre-run snapshot — the existing escape
    hatch for a night's consolidation that looks wrong."""
    _audit("consolidation_rollback", user=user_id, run=run_id)
    from consolidation import rollback
    if rollback(user_id, run_id):
        return _json({"status": "ok", "result": f"profile restored to pre-run snapshot of run {run_id}"})
    return _json({"status": "error", "message": "run not found, wrong user, or no snapshot"}, 400)

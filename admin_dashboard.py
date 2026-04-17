"""
Admin Dashboard — Cued
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cued — Admin</title>
<style>
:root{
  --bg:#050506;--bg2:#0A0A0C;--surface:#111114;--card:#19191D;
  --border:#1F1F24;--text:#F5F5F7;--text2:#A1A1A6;--text3:#6E6E73;
  --accent:#7C6EFF;--green:#30D158;--yellow:#FFD60A;--red:#FF453A;--blue:#0A84FF;
  --sidebar:220px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);display:flex;min-height:100vh}

/* ── SIDEBAR ── */
.sidebar{width:var(--sidebar);background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;position:fixed;top:0;left:0;height:100vh;overflow-y:auto;z-index:10}
.sidebar-logo{padding:24px 20px 16px;border-bottom:1px solid var(--border)}
.sidebar-logo .wordmark{font-size:18px;font-weight:700;letter-spacing:-.5px}
.sidebar-logo .sub{font-size:11px;color:var(--text3);margin-top:2px}
.nav-section{padding:16px 12px 4px;font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;margin:2px 8px;cursor:pointer;font-size:13px;font-weight:500;color:var(--text2);transition:all .15s;border:none;background:none;width:calc(100% - 16px);text-align:left}
.nav-item:hover{background:rgba(255,255,255,.05);color:var(--text)}
.nav-item.active{background:rgba(124,110,255,.15);color:var(--accent)}
.nav-item .icon{width:16px;text-align:center;flex-shrink:0;font-size:14px}
.nav-item .badge-count{margin-left:auto;background:var(--accent);color:#fff;border-radius:10px;padding:1px 6px;font-size:10px;font-weight:700}
.sidebar-footer{margin-top:auto;padding:16px;border-top:1px solid var(--border);font-size:11px;color:var(--text3)}

/* ── MAIN ── */
.main{margin-left:var(--sidebar);flex:1;padding:32px;max-width:calc(100vw - var(--sidebar))}
.page{display:none}
.page.active{display:block}
.page-header{margin-bottom:28px}
.page-header h1{font-size:22px;font-weight:700;letter-spacing:-.4px}
.page-header p{color:var(--text3);font-size:13px;margin-top:4px}

/* ── STAT GRID ── */
.grid{display:grid;gap:12px;margin-bottom:28px}
.grid-4{grid-template-columns:repeat(4,1fr)}
.grid-3{grid-template-columns:repeat(3,1fr)}
.grid-2{grid-template-columns:repeat(2,1fr)}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.stat-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px}
.stat-val{font-size:30px;font-weight:700;letter-spacing:-1px;line-height:1}
.stat-sub{font-size:11px;color:var(--text3);margin-top:5px}
.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}.blue{color:var(--blue)}.accent{color:var(--accent)}

/* ── SECTION ── */
.section{margin-bottom:32px}
.section-title{font-size:13px;font-weight:600;color:var(--text2);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── TABLE ── */
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:1px;padding:10px 16px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid var(--border);color:var(--text2)}
tr:last-child td{border-bottom:none}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:rgba(124,110,255,.05)}
.empty{color:var(--text3);font-size:13px;padding:32px;text-align:center}

/* ── BADGE ── */
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.3px}
.badge-green{background:rgba(48,209,88,.12);color:var(--green)}
.badge-yellow{background:rgba(255,214,10,.12);color:var(--yellow)}
.badge-red{background:rgba(255,69,58,.12);color:var(--red)}
.badge-gray{background:rgba(110,110,115,.12);color:var(--text3)}
.badge-blue{background:rgba(10,132,255,.12);color:var(--blue)}
.badge-accent{background:rgba(124,110,255,.12);color:var(--accent)}

/* ── MESSAGES FEED ── */
.msg-feed{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;max-height:520px;overflow-y:auto}
.msg-item{padding:10px 16px;border-bottom:1px solid var(--border);display:grid;grid-template-columns:130px 100px 18px 1fr 80px;gap:8px;font-size:12px;cursor:pointer;transition:background .15s;align-items:center}
.msg-item:last-child{border-bottom:none}
.msg-item:hover{background:rgba(124,110,255,.05)}
.msg-time{color:var(--text3);font-size:11px}
.msg-user{color:var(--accent);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg-body{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg-type{color:var(--text3);font-size:10px;text-align:right}

/* ── FORMS ── */
.form-row{display:flex;gap:8px;align-items:flex-start}
.form-row select,.form-row textarea,.form-row input{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:13px;font-family:inherit}
.form-row select{flex-shrink:0}
.form-row textarea{flex:1;min-height:40px;resize:none}
.btn{border:none;border-radius:8px;padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.85}
.btn-primary{background:var(--accent);color:#fff}
.btn-danger{background:rgba(255,69,58,.12);color:var(--red)}
.btn-sm{padding:4px 10px;font-size:11px;border-radius:5px}

/* ── CHART BAR ── */
.bar-chart{display:flex;gap:10px;align-items:flex-end;height:80px;padding:0 4px}
.bar-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}
.bar-col .bar-count{font-size:11px;color:var(--text3)}
.bar-col .bar{width:100%;border-radius:3px 3px 0 0;min-height:2px;transition:height .3s}
.bar-col .bar-label{font-size:12px;font-weight:600;color:var(--text2)}

/* ── AGENT PIPELINE BADGES ── */
.pipeline{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.pipe-badge{font-size:10px;padding:2px 7px;border-radius:4px;font-weight:500}

/* ── REFRESH BTN ── */
.fab{position:fixed;bottom:24px;right:24px;background:var(--accent);color:#fff;border:none;border-radius:980px;padding:11px 20px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 20px rgba(124,110,255,.3);z-index:100}
.fab:hover{opacity:.9}

/* ── INFO CARD ── */
.info-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:12px}
.info-row{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;border-bottom:1px solid var(--border);font-size:13px}
.info-row:last-child{border-bottom:none}
.info-key{color:var(--text3)}
.info-val{color:var(--text);font-weight:500;text-align:right;max-width:60%}

/* ── NOTICE ── */
.notice{background:rgba(255,214,10,.07);border:1px solid rgba(255,214,10,.2);border-radius:8px;padding:12px 16px;font-size:12px;color:var(--yellow);margin-bottom:20px}
</style>
</head>
<body>

<!-- ═══ SIDEBAR ═══ -->
<nav class="sidebar">
  <div class="sidebar-logo">
    <div class="wordmark">cued</div>
    <div class="sub">Admin Console</div>
  </div>

  <div class="nav-section">Overview</div>
  <button class="nav-item active" onclick="showPage('metrics',this)">
    <span class="icon">◈</span> Metrics
  </button>

  <div class="nav-section">Data</div>
  <button class="nav-item" onclick="showPage('users',this)">
    <span class="icon">◉</span> Users
    <span class="badge-count">{{ total_users }}</span>
  </button>
  <button class="nav-item" onclick="showPage('messages',this)">
    <span class="icon">◎</span> Messages
  </button>
  <button class="nav-item" onclick="showPage('meals',this)">
    <span class="icon">◍</span> Meals Logged
  </button>

  <div class="nav-section">Operations</div>
  <button class="nav-item" onclick="showPage('send',this)">
    <span class="icon">▷</span> Manual Send
  </button>
  <button class="nav-item" onclick="showPage('finances',this)">
    <span class="icon">◈</span> Finances
  </button>

  <div class="nav-section">System</div>
  <button class="nav-item" onclick="showPage('pipeline',this)">
    <span class="icon">⟡</span> Agent Pipeline
  </button>
  <button class="nav-item" onclick="showPage('retention',this)">
    <span class="icon">◈</span> Retention
  </button>

  <div class="sidebar-footer">
    Last refreshed<br>{{ now }}
  </div>
</nav>

<!-- ═══ MAIN ═══ -->
<main class="main">

<!-- ─── METRICS ─── -->
<div id="page-metrics" class="page active">
  <div class="page-header">
    <h1>Metrics</h1>
    <p>Live beta overview — refreshes every 60s</p>
  </div>

  <div class="grid grid-4">
    <div class="stat-card">
      <div class="stat-label">Total Users</div>
      <div class="stat-val accent">{{ total_users }}</div>
      <div class="stat-sub">{{ active_users }} active</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Active Today</div>
      <div class="stat-val blue">{{ today_active }}</div>
      <div class="stat-sub">users who texted in</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Response Rate</div>
      <div class="stat-val {{ 'green' if response_rate >= 70 else 'yellow' if response_rate >= 40 else 'red' }}">{{ response_rate }}%</div>
      <div class="stat-sub">{{ total_received }} in / {{ total_sent }} out</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Meals Logged</div>
      <div class="stat-val green">{{ total_meals }}</div>
      <div class="stat-sub">{{ meals_today }} today</div>
    </div>
  </div>

  <div class="grid grid-4">
    <div class="stat-card">
      <div class="stat-label">Messages Sent</div>
      <div class="stat-val">{{ total_sent }}</div>
      <div class="stat-sub">by coach (outbound)</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Messages Received</div>
      <div class="stat-val">{{ total_received }}</div>
      <div class="stat-sub">from users</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Day Rating</div>
      <div class="stat-val {{ 'green' if avg_rating >= 4 else 'yellow' if avg_rating >= 3 else 'red' if avg_rating > 0 else '' }}">{{ avg_rating if avg_rating > 0 else '—' }}</div>
      <div class="stat-sub">{{ total_ratings }} ratings submitted</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Weight Logs</div>
      <div class="stat-val">{{ total_weight_logs }}</div>
      <div class="stat-sub">weigh-ins recorded</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Day Rating Distribution</div>
    {% if total_ratings > 0 %}
    <div class="info-card">
      <div class="bar-chart">
        {% for i in range(1,6) %}
        <div class="bar-col">
          <div class="bar-count">{{ rating_counts[i] }}</div>
          <div class="bar" style="height:{{ (rating_counts[i] / max_rating_count * 60) if max_rating_count > 0 else 0 }}px;background:{{ 'var(--red)' if i <= 2 else 'var(--yellow)' if i == 3 else 'var(--green)' }}"></div>
          <div class="bar-label">{{ i }}</div>
        </div>
        {% endfor %}
      </div>
    </div>
    {% else %}<p class="empty">No ratings submitted yet.</p>{% endif %}
  </div>

  <div class="section">
    <div class="section-title">Recent Activity</div>
    <div class="msg-feed">
      {% for m in recent_messages[:20] %}
      <div class="msg-item" onclick="window.location.href='/admin/user/{{ m.user_id }}'">
        <div class="msg-time">{{ m.time }}</div>
        <div class="msg-user">{{ m.user_name }}</div>
        <div>{{ '→' if m.direction == 'out' else '←' }}</div>
        <div class="msg-body">{{ m.body[:100] }}{{ '…' if m.body|length > 100 else '' }}</div>
        <div class="msg-type">{{ m.message_type }}</div>
      </div>
      {% endfor %}
      {% if not recent_messages %}<p class="empty">No messages yet.</p>{% endif %}
    </div>
  </div>
</div>

<!-- ─── USERS ─── -->
<div id="page-users" class="page">
  <div class="page-header">
    <h1>Users</h1>
    <p>Click a row to view full profile and conversation</p>
  </div>
  <div class="table-wrap">
    <table>
      <tr>
        <th>Name</th><th>Phone</th><th>Signed Up</th><th>Last Active</th>
        <th>Messages</th><th>Meals</th><th>Workouts</th><th>Avg Rating</th>
        <th>Onboarding</th><th>Status</th><th></th>
      </tr>
      {% for u in users %}
      <tr class="clickable" onclick="window.location.href='/admin/user/{{ u.id }}'">
        <td style="color:var(--text);font-weight:500">{{ u.name }}</td>
        <td>···{{ u.phone }}</td>
        <td>{{ u.signed_up }}</td>
        <td>{{ u.last_active }}</td>
        <td>{{ u.msg_count }}</td>
        <td>{{ u.meal_count }}</td>
        <td>{{ u.workout_count }}</td>
        <td>{{ u.avg_rating }}</td>
        <td>
          {% if u.onboarding_step >= 4 %}<span class="badge badge-green">DONE</span>
          {% elif u.onboarding_step > 0 %}<span class="badge badge-yellow">STEP {{ u.onboarding_step }}</span>
          {% else %}<span class="badge badge-gray">NOT STARTED</span>{% endif %}
        </td>
        <td>
          {% if u.days_inactive == 0 %}<span class="badge badge-green">ACTIVE TODAY</span>
          {% elif u.days_inactive <= 2 %}<span class="badge badge-green">ACTIVE</span>
          {% elif u.days_inactive <= 7 %}<span class="badge badge-yellow">QUIET</span>
          {% else %}<span class="badge badge-red">{{ u.days_inactive }}d SILENT</span>{% endif %}
        </td>
        <td onclick="event.stopPropagation()">
          <button class="btn btn-danger btn-sm" onclick="deleteUser({{ u.id }},'{{ u.name }}')">Delete</button>
        </td>
      </tr>
      {% endfor %}
      {% if not users %}<tr><td colspan="11" class="empty">No users yet.</td></tr>{% endif %}
    </table>
  </div>
</div>

<!-- ─── MESSAGES ─── -->
<div id="page-messages" class="page">
  <div class="page-header">
    <h1>Messages</h1>
    <p>All {{ total_sent + total_received }} messages — click a row to open conversation</p>
  </div>

  <div class="grid grid-3" style="margin-bottom:20px">
    <div class="stat-card">
      <div class="stat-label">Outbound (Coach)</div>
      <div class="stat-val">{{ total_sent }}</div>
      <div class="stat-sub">sent to users</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Inbound (Users)</div>
      <div class="stat-val">{{ total_received }}</div>
      <div class="stat-sub">received from users</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Response Rate</div>
      <div class="stat-val {{ 'green' if response_rate >= 70 else 'yellow' if response_rate >= 40 else 'red' }}">{{ response_rate }}%</div>
      <div class="stat-sub">replies per coach message</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Recent Messages (last 50)</div>
    <div class="msg-feed">
      {% for m in recent_messages %}
      <div class="msg-item" onclick="window.location.href='/admin/user/{{ m.user_id }}'">
        <div class="msg-time">{{ m.time }}</div>
        <div class="msg-user">{{ m.user_name }}</div>
        <div>{{ '→' if m.direction == 'out' else '←' }}</div>
        <div class="msg-body">{{ m.body[:100] }}{{ '…' if m.body|length > 100 else '' }}</div>
        <div class="msg-type">{{ m.message_type }}</div>
      </div>
      {% endfor %}
      {% if not recent_messages %}<p class="empty">No messages yet.</p>{% endif %}
    </div>
  </div>
</div>

<!-- ─── MEALS ─── -->
<div id="page-meals" class="page">
  <div class="page-header">
    <h1>Meals Logged</h1>
    <p>All meals captured via conversation</p>
  </div>

  <div class="grid grid-3" style="margin-bottom:20px">
    <div class="stat-card">
      <div class="stat-label">Total Meals</div>
      <div class="stat-val green">{{ total_meals }}</div>
      <div class="stat-sub">across all users</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Logged Today</div>
      <div class="stat-val">{{ meals_today }}</div>
      <div class="stat-sub">meals so far today</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Calories</div>
      <div class="stat-val">{{ avg_meal_calories }}</div>
      <div class="stat-sub">per logged meal</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Recent Meals</div>
    <div class="table-wrap">
      <table>
        <tr><th>User</th><th>When</th><th>Meal</th><th>Cal</th><th>Protein</th><th>Source</th><th>Confidence</th></tr>
        {% for m in recent_meals %}
        <tr class="clickable" onclick="window.location.href='/admin/user/{{ m.user_id }}'">
          <td style="color:var(--accent);font-weight:500">{{ m.user_name }}</td>
          <td>{{ m.eaten_at }}</td>
          <td>{{ m.description[:60] }}{{ '…' if m.description|length > 60 else '' }}</td>
          <td>{{ m.calories }}</td>
          <td>{{ m.protein_g }}g</td>
          <td><span class="badge badge-{{ 'blue' if m.source == 'photo' else 'gray' }}">{{ m.source }}</span></td>
          <td><span class="badge badge-{{ 'green' if m.confidence == 'high' else 'yellow' if m.confidence == 'medium' else 'gray' }}">{{ m.confidence }}</span></td>
        </tr>
        {% endfor %}
        {% if not recent_meals %}<tr><td colspan="7" class="empty">No meals logged yet.</td></tr>{% endif %}
      </table>
    </div>
  </div>
</div>

<!-- ─── MANUAL SEND ─── -->
<div id="page-send" class="page">
  <div class="page-header">
    <h1>Manual Send</h1>
    <p>Admin override — send a message as the coach to any user</p>
  </div>
  <div class="notice">⚠ This bypasses the AI entirely. Message will appear to come from the coach and log as outbound.</div>
  <div class="info-card">
    <form onsubmit="return sendManual(event)" style="display:flex;flex-direction:column;gap:12px">
      <div class="form-row">
        <select id="send-user" style="flex:1">
          {% for u in users %}<option value="{{ u.id }}">{{ u.name }} (···{{ u.phone }})</option>{% endfor %}
        </select>
      </div>
      <div class="form-row">
        <textarea id="send-body" placeholder="Type a message to send as coach…" style="flex:1;min-height:80px"></textarea>
      </div>
      <div>
        <button type="submit" class="btn btn-primary">Send Message</button>
      </div>
    </form>
  </div>
</div>

<!-- ─── FINANCES ─── -->
<div id="page-finances" class="page">
  <div class="page-header">
    <h1>Finances</h1>
    <p>Estimated costs this month — based on usage volume</p>
  </div>
  <div class="grid grid-3">
    <div class="stat-card">
      <div class="stat-label">Twilio (SMS)</div>
      <div class="stat-val">${{ twilio_cost }}</div>
      <div class="stat-sub">~$0.015/segment × {{ total_sent + total_received }} msgs</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Anthropic (API)</div>
      <div class="stat-val">${{ api_cost }}</div>
      <div class="stat-sub">~$0.006/call × {{ total_sent }} coach calls</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Burn</div>
      <div class="stat-val red">${{ total_cost }}</div>
      <div class="stat-sub">this month (estimate)</div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">Cost Per User</div>
    <div class="grid grid-2">
      <div class="stat-card">
        <div class="stat-label">Cost / Active User</div>
        <div class="stat-val">${{ cost_per_user }}</div>
        <div class="stat-sub">total burn ÷ active users</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Cost / Message</div>
        <div class="stat-val">${{ cost_per_msg }}</div>
        <div class="stat-sub">total burn ÷ total messages</div>
      </div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">Notes</div>
    <div class="info-card">
      <div class="info-row"><span class="info-key">Twilio rate</span><span class="info-val">$0.0079/SMS segment (US), ~$0.015 fully loaded</span></div>
      <div class="info-row"><span class="info-key">Anthropic rate</span><span class="info-val">~$0.006/response (Haiku + Sonnet blended estimate)</span></div>
      <div class="info-row"><span class="info-key">Railway hosting</span><span class="info-val">Not included — check Railway dashboard</span></div>
      <div class="info-row"><span class="info-key">Reset cadence</span><span class="info-val">Estimates are cumulative (all-time), not calendar month</span></div>
    </div>
  </div>
</div>

<!-- ─── AGENT PIPELINE ─── -->
<div id="page-pipeline" class="page">
  <div class="page-header">
    <h1>Agent Pipeline</h1>
    <p>Routing breakdown — where messages are going</p>
  </div>
  <div class="grid grid-4">
    <div class="stat-card">
      <div class="stat-label">Nutrition Agent</div>
      <div class="stat-val green">{{ route_nutrition }}</div>
      <div class="stat-sub">messages routed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Training Agent</div>
      <div class="stat-val blue">{{ route_training }}</div>
      <div class="stat-sub">messages routed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Readiness Agent</div>
      <div class="stat-val accent">{{ route_readiness }}</div>
      <div class="stat-sub">messages routed</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Legacy (Personality)</div>
      <div class="stat-val">{{ route_legacy }}</div>
      <div class="stat-sub">messages to monolith</div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">Message Type Breakdown</div>
    <div class="table-wrap">
      <table>
        <tr><th>Message Type</th><th>Count</th><th>Share</th></tr>
        {% for mt in message_types %}
        <tr>
          <td>{{ mt.type }}</td>
          <td>{{ mt.count }}</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px">
              <div style="height:6px;width:{{ mt.pct }}px;background:var(--accent);border-radius:3px;max-width:120px"></div>
              <span>{{ mt.pct }}%</span>
            </div>
          </td>
        </tr>
        {% endfor %}
        {% if not message_types %}<tr><td colspan="3" class="empty">No messages yet.</td></tr>{% endif %}
      </table>
    </div>
  </div>
</div>

<!-- ─── RETENTION ─── -->
<div id="page-retention" class="page">
  <div class="page-header">
    <h1>Retention</h1>
    <p>How many users stay active over time</p>
  </div>
  <div class="table-wrap" style="margin-bottom:24px">
    <table>
      <tr><th>Cohort</th><th>Users</th><th>Rate</th><th>Benchmark</th><th>Status</th></tr>
      <tr>
        <td>Day 1 — signed up</td>
        <td>{{ total_users }}</td><td>100%</td><td>—</td>
        <td><span class="badge badge-gray">BASELINE</span></td>
      </tr>
      <tr>
        <td>Day 1 — replied to first message</td>
        <td>{{ d1_responded }}</td><td>{{ d1_rate }}%</td><td>&gt;70%</td>
        <td><span class="badge {{ 'badge-green' if d1_rate >= 70 else 'badge-yellow' if d1_rate >= 40 else 'badge-red' }}">{{ 'STRONG' if d1_rate >= 70 else 'OK' if d1_rate >= 40 else 'WEAK' }}</span></td>
      </tr>
      <tr>
        <td>Day 7 — active in last 7 days</td>
        <td>{{ d7_active }} / {{ d7_eligible }}</td><td>{{ d7_rate }}%</td><td>&gt;50%</td>
        <td><span class="badge {{ 'badge-green' if d7_rate >= 50 else 'badge-yellow' if d7_rate >= 25 else 'badge-red' }}">{{ 'STRONG' if d7_rate >= 50 else 'OK' if d7_rate >= 25 else 'WEAK' }}</span></td>
      </tr>
      <tr>
        <td>Day 14 — active in last 14 days</td>
        <td>{{ d14_active }} / {{ d14_eligible }}</td><td>{{ d14_rate }}%</td><td>&gt;40%</td>
        <td><span class="badge {{ 'badge-green' if d14_rate >= 40 else 'badge-yellow' if d14_rate >= 20 else 'badge-red' }}">{{ 'STRONG' if d14_rate >= 40 else 'OK' if d14_rate >= 20 else 'WEAK' }}</span></td>
      </tr>
      <tr>
        <td>Day 30 — active in last 30 days</td>
        <td>{{ d30_active }} / {{ d30_eligible }}</td><td>{{ d30_rate }}%</td><td>&gt;30%</td>
        <td><span class="badge {{ 'badge-green' if d30_rate >= 30 else 'badge-yellow' if d30_rate >= 10 else 'badge-red' }}">{{ 'STRONG' if d30_rate >= 30 else 'OK' if d30_rate >= 10 else 'WEAK' }}</span></td>
      </tr>
    </table>
  </div>
  <p style="color:var(--text3);font-size:11px">Industry average day-30 retention: 3%. Coaching apps with high personalization can reach 40–60%. That's the target.</p>
</div>

</main>

<button class="fab" onclick="location.reload()">↻ Refresh</button>

<script>
function showPage(id, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  btn.classList.add('active');
}

async function deleteUser(userId, name) {
  if (!confirm('Delete ' + name + '? This removes all their data. Cannot be undone.')) return;
  const res = await fetch('/admin/user/' + userId + '/delete', {method: 'POST'});
  const data = await res.json();
  if (data.status === 'ok') location.reload();
  else alert('Error: ' + data.message);
}

async function sendManual(e) {
  e.preventDefault();
  const userId = document.getElementById('send-user').value;
  const body = document.getElementById('send-body').value;
  if (!body.trim()) return false;
  const res = await fetch('/admin/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'user_id=' + userId + '&body=' + encodeURIComponent(body)
  });
  document.getElementById('send-body').value = '';
  alert('Sent.');
  return false;
}

setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>
"""

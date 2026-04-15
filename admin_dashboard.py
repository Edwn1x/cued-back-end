"""
Admin Dashboard — Cued
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cued — Admin Console</title>
<style>
:root{--bg:#050506;--bg2:#0A0A0C;--surface:#111114;--card:#19191D;--border:#1F1F24;--text:#F5F5F7;--text2:#A1A1A6;--text3:#6E6E73;--accent:#7C6EFF;--green:#30D158;--yellow:#FFD60A;--red:#FF453A;--blue:#0A84FF}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:24px;font-weight:700;margin-bottom:4px;letter-spacing:-.5px}
.sub{color:var(--text3);font-size:13px;margin-bottom:32px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:32px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.stat-label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px}
.stat-val{font-size:32px;font-weight:700;letter-spacing:-1px}
.stat-sub{font-size:12px;color:var(--text3);margin-top:4px}
.green{color:var(--green)}.yellow{color:var(--yellow)}.red{color:var(--red)}.blue{color:var(--blue)}.accent{color:var(--accent)}
.section{margin-bottom:32px}
.section-title{font-size:14px;font-weight:600;color:var(--text2);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--text3);font-size:11px;text-transform:uppercase;letter-spacing:1px;padding:10px 12px;border-bottom:1px solid var(--border)}
td{padding:10px 12px;border-bottom:1px solid var(--border);color:var(--text2)}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:rgba(124,110,255,.05)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600}
.badge-green{background:rgba(48,209,88,.12);color:var(--green)}
.badge-yellow{background:rgba(255,214,10,.12);color:var(--yellow)}
.badge-red{background:rgba(255,69,58,.12);color:var(--red)}
.badge-gray{background:rgba(110,110,115,.12);color:var(--text3)}
.msg-feed{max-height:500px;overflow-y:auto}
.msg-item{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;gap:12px;font-size:13px;cursor:pointer;transition:background .15s}
.msg-item:hover{background:rgba(124,110,255,.05)}
.msg-time{color:var(--text3);font-size:11px;min-width:130px;flex-shrink:0}
.msg-user{color:var(--accent);min-width:80px;flex-shrink:0;font-weight:500}
.msg-dir{min-width:20px;flex-shrink:0}
.msg-body{color:var(--text2);flex:1}
.msg-type{color:var(--text3);font-size:10px;min-width:80px;text-align:right;flex-shrink:0}
.send-form{display:flex;gap:8px;padding:12px;background:var(--card);border-radius:8px;margin-top:8px}
.send-form select,.send-form textarea{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px;font-size:13px;font-family:inherit}
.send-form select{width:140px}
.send-form textarea{flex:1;min-height:36px;resize:none}
.send-form button{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
.send-form button:hover{opacity:.9}
.refresh-btn{position:fixed;bottom:24px;right:24px;background:var(--accent);color:#fff;border:none;border-radius:980px;padding:12px 20px;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 4px 20px rgba(124,110,255,.3)}
.empty{color:var(--text3);font-size:13px;padding:24px;text-align:center}
</style>
</head>
<body>

<h1>cued <span style="color:var(--text3);font-weight:400">admin</span></h1>
<p class="sub">Beta metrics &middot; Last refreshed: {{ now }}</p>

<div class="grid">
  <div class="stat-card"><div class="stat-label">Total Users</div><div class="stat-val accent">{{ total_users }}</div><div class="stat-sub">{{ active_users }} active</div></div>
  <div class="stat-card"><div class="stat-label">Messages Sent</div><div class="stat-val">{{ total_sent }}</div><div class="stat-sub">by coach</div></div>
  <div class="stat-card"><div class="stat-label">Messages Received</div><div class="stat-val">{{ total_received }}</div><div class="stat-sub">from users</div></div>
  <div class="stat-card"><div class="stat-label">Response Rate</div><div class="stat-val {{ 'green' if response_rate >= 70 else 'yellow' if response_rate >= 40 else 'red' }}">{{ response_rate }}%</div><div class="stat-sub">user replies / coach messages</div></div>
  <div class="stat-card"><div class="stat-label">Avg Day Rating</div><div class="stat-val {{ 'green' if avg_rating >= 4 else 'yellow' if avg_rating >= 3 else 'red' }}">{{ avg_rating }}</div><div class="stat-sub">out of 5 ({{ total_ratings }} ratings)</div></div>
  <div class="stat-card"><div class="stat-label">Today Active</div><div class="stat-val blue">{{ today_active }}</div><div class="stat-sub">users who texted today</div></div>
</div>

<div class="section">
  <div class="section-title">Retention Cohorts</div>
  <table>
    <tr><th>Metric</th><th>Count</th><th>Rate</th><th>Status</th></tr>
    <tr><td>Day 1 (signed up)</td><td>{{ total_users }}</td><td>100%</td><td><span class="badge badge-green">BASELINE</span></td></tr>
    <tr><td>Day 1 (responded to first msg)</td><td>{{ d1_responded }}</td><td>{{ d1_rate }}%</td><td><span class="badge {{ 'badge-green' if d1_rate >= 70 else 'badge-yellow' if d1_rate >= 40 else 'badge-red' }}">{{ 'STRONG' if d1_rate >= 70 else 'OK' if d1_rate >= 40 else 'WEAK' }}</span></td></tr>
    <tr><td>Day 7 (active in last 7 days)</td><td>{{ d7_active }}</td><td>{{ d7_rate }}%</td><td><span class="badge {{ 'badge-green' if d7_rate >= 50 else 'badge-yellow' if d7_rate >= 25 else 'badge-red' }}">{{ 'STRONG' if d7_rate >= 50 else 'OK' if d7_rate >= 25 else 'WEAK' }}</span></td></tr>
    <tr><td>Day 14 (active in last 14 days)</td><td>{{ d14_active }}</td><td>{{ d14_rate }}%</td><td><span class="badge {{ 'badge-green' if d14_rate >= 40 else 'badge-yellow' if d14_rate >= 20 else 'badge-red' }}">{{ 'STRONG' if d14_rate >= 40 else 'OK' if d14_rate >= 20 else 'WEAK' }}</span></td></tr>
    <tr><td>Day 30 (active in last 30 days)</td><td>{{ d30_active }}</td><td>{{ d30_rate }}%</td><td><span class="badge {{ 'badge-green' if d30_rate >= 30 else 'badge-yellow' if d30_rate >= 10 else 'badge-red' }}">{{ 'STRONG' if d30_rate >= 30 else 'OK' if d30_rate >= 10 else 'WEAK' }}</span></td></tr>
  </table>
  <p style="color:var(--text3);font-size:11px;margin-top:8px">Industry average day-30 retention: 3%. Target: &gt;50%.</p>
</div>

<div class="section">
  <div class="section-title">Users <span style="color:var(--text3);font-size:11px;font-weight:400;margin-left:4px">&mdash; click a row to view full profile &amp; conversation</span></div>
  <table>
    <tr><th>Name</th><th>Phone</th><th>Signed Up</th><th>Last Active</th><th>Messages</th><th>Workouts</th><th>Avg Rating</th><th>Status</th><th></th></tr>
    {% for u in users %}
    <tr class="clickable" onclick="window.location.href='/admin/user/{{ u.id }}'">
      <td style="color:var(--text);font-weight:500">{{ u.name }}</td>
      <td>&middot;&middot;&middot;{{ u.phone }}</td>
      <td>{{ u.signed_up }}</td>
      <td>{{ u.last_active }}</td>
      <td>{{ u.msg_count }}</td>
      <td>{{ u.workout_count }}</td>
      <td>{{ u.avg_rating }}</td>
      <td>
        {% if u.days_inactive == 0 %}<span class="badge badge-green">ACTIVE TODAY</span>
        {% elif u.days_inactive <= 2 %}<span class="badge badge-green">ACTIVE</span>
        {% elif u.days_inactive <= 7 %}<span class="badge badge-yellow">QUIET</span>
        {% else %}<span class="badge badge-red">INACTIVE {{ u.days_inactive }}d</span>{% endif %}
      </td>
      <td onclick="event.stopPropagation()">
        <button onclick="deleteUser({{ u.id }}, '{{ u.name }}')" style="background:rgba(255,69,58,.12);color:var(--red);border:none;border-radius:4px;padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer">Delete</button>
      </td>
    </tr>
    {% endfor %}
    {% if not users %}<tr><td colspan="9" class="empty">No users yet.</td></tr>{% endif %}
  </table>
</div>

<div class="section">
  <div class="section-title">Day Rating Distribution</div>
  {% if total_ratings > 0 %}
  <div style="display:flex;gap:12px;align-items:end;height:80px;padding:0 20px">
    {% for i in range(1,6) %}
    <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px">
      <div style="font-size:11px;color:var(--text3)">{{ rating_counts[i] }}</div>
      <div style="width:100%;height:{{ (rating_counts[i] / max_rating_count * 60) if max_rating_count > 0 else 0 }}px;background:{{ 'var(--red)' if i <= 2 else 'var(--yellow)' if i == 3 else 'var(--green)' }};border-radius:3px 3px 0 0;min-height:2px"></div>
      <div style="font-size:12px;font-weight:600;color:var(--text2)">{{ i }}</div>
    </div>
    {% endfor %}
  </div>
  {% else %}<p class="empty">No ratings yet.</p>{% endif %}
</div>

<div class="section">
  <div class="section-title">Recent Messages <span style="color:var(--text3);font-size:11px;font-weight:400;margin-left:4px">&mdash; click to open full conversation</span></div>
  <div class="msg-feed">
    {% for m in recent_messages %}
    <div class="msg-item" onclick="window.location.href='/admin/user/{{ m.user_id }}'">
      <div class="msg-time">{{ m.time }}</div>
      <div class="msg-user">{{ m.user_name }}</div>
      <div class="msg-dir">{{ '&rarr;' if m.direction == 'out' else '&larr;' }}</div>
      <div class="msg-body">{{ m.body[:120] }}{{ '...' if m.body|length > 120 else '' }}</div>
      <div class="msg-type">{{ m.message_type }}</div>
    </div>
    {% endfor %}
    {% if not recent_messages %}<p class="empty">No messages yet.</p>{% endif %}
  </div>
</div>

<div class="section">
  <div class="section-title">Manual Send (Admin Override)</div>
  <form class="send-form" onsubmit="return sendManual(event)">
    <select id="send-user">{% for u in users %}<option value="{{ u.id }}">{{ u.name }}</option>{% endfor %}</select>
    <textarea id="send-body" placeholder="Type a message to send as coach..."></textarea>
    <button type="submit">Send</button>
  </form>
</div>

<div class="section">
  <div class="section-title">Estimated Costs (This Month)</div>
  <div class="grid" style="grid-template-columns:repeat(3,1fr)">
    <div class="stat-card"><div class="stat-label">Twilio (SMS)</div><div class="stat-val" style="font-size:24px">${{ twilio_cost }}</div><div class="stat-sub">~$0.015/segment &times; {{ total_sent + total_received }} msgs</div></div>
    <div class="stat-card"><div class="stat-label">Anthropic (API)</div><div class="stat-val" style="font-size:24px">${{ api_cost }}</div><div class="stat-sub">~$0.006/call &times; {{ total_sent }} calls</div></div>
    <div class="stat-card"><div class="stat-label">Total Burn</div><div class="stat-val red" style="font-size:24px">${{ total_cost }}</div><div class="stat-sub">this month</div></div>
  </div>
</div>

<button class="refresh-btn" onclick="location.reload()">&#x21BB; Refresh</button>

<script>
async function deleteUser(userId, name){
  if(!confirm('Delete ' + name + '? This removes all their messages, workouts, and data. Cannot be undone.'))return;
  const res = await fetch('/admin/user/'+userId+'/delete',{method:'POST'});
  const data = await res.json();
  if(data.status==='ok'){location.reload();}
  else{alert('Error: '+data.message);}
}
async function sendManual(e){
  e.preventDefault();
  const userId=document.getElementById('send-user').value;
  const body=document.getElementById('send-body').value;
  if(!body.trim())return false;
  await fetch('/admin/send',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'user_id='+userId+'&body='+encodeURIComponent(body)});
  document.getElementById('send-body').value='';
  location.reload();
  return false;
}
setTimeout(()=>location.reload(),60000);
</script>
</body>
</html>
"""

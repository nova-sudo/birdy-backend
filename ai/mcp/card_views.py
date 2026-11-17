"""
ai/mcp/card_views.py
--------------------
The HTML of Birdy's cards (see ai/mcp/cards.py). `page()` wraps a card body in the shared shell: Birdy's tokens
(light and dark, following the host's theme), the mascot in its round frame, and the SDK wiring. A body defines
`window.birdyRender(data, ui)`, called with the tool's structuredContent. `ui.open(path)` opens a Birdy page,
`ui.args` holds the tool call's arguments, `ui.call(tool, args)` calls a Birdy tool (as the same user, through the
same access checks and charges), and `ui.tell(text)` tells the assistant what the user did in the card.
"""

from core.config import FRONTEND_URL

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#FFFFFF;--soft:#F6F6F4;--ink:#18181B;--body:#52525B;--faint:#6B6B72;--border:#E7E6E3;--divider:#EFEEEB;
--primary:#5B3FD6;--tint:#EFECFB;--pale:#C3B7F2;--good:#16794C;--good-bg:#E8F4EE;--bad:#D93A40;--bad-bg:#FEF1F1;
--warn:#B4530A;--warn-bg:#FDF1E7;--on-primary:#FFFFFF;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1C1C1F;--soft:#242428;--ink:#F2F2F3;--body:#B9B9BF;
--faint:#8E8E95;--border:#2E2E33;--divider:#29292D;--primary:#A08CF2;--tint:#2A2546;--pale:#4A3F82;--good:#4CC38A;
--good-bg:#16291F;--bad:#FF6B70;--bad-bg:#331A1C;--warn:#F0A35E;--warn-bg:#33241A;--on-primary:#18181B;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#1C1C1F;--soft:#242428;--ink:#F2F2F3;--body:#B9B9BF;--faint:#8E8E95;--border:#2E2E33;
--divider:#29292D;--primary:#A08CF2;--tint:#2A2546;--pale:#4A3F82;--good:#4CC38A;--good-bg:#16291F;--bad:#FF6B70;
--bad-bg:#331A1C;--warn:#F0A35E;--warn-bg:#33241A;--on-primary:#18181B;color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.card{padding:14px;border:1px solid var(--border);border-radius:12px;background:var(--bg)}
.hd{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.hd b{font-size:15px;flex:1;min-width:0}
.hd small{color:var(--faint);font-size:11.5px}
.av{width:24px;height:24px;border-radius:50%;background:var(--tint) center/cover no-repeat;border:1.5px solid var(--pale);flex:none}
.pill{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;border-radius:999px;padding:2px 8px;white-space:nowrap}
.pill.act{background:var(--bad-bg);color:var(--bad)}.pill.watch{background:var(--warn-bg);color:var(--warn)}.pill.fine{background:var(--good-bg);color:var(--good)}
.n{font-variant-numeric:tabular-nums}
.up{color:var(--good)}.dn{color:var(--bad)}.flat{color:var(--faint)}
button.link{all:unset;cursor:pointer;color:var(--primary);font-weight:600}
button.link:focus-visible,.tile:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
.empty{color:var(--faint);padding:8px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.kpi{background:var(--soft);border-radius:9px;padding:8px 10px;min-width:0}
.kpi i{font-style:normal;font-size:10.5px;color:var(--faint);display:block;text-transform:uppercase;letter-spacing:.04em}
.kpi strong{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi em{font-style:normal;font-size:11.5px;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border);border-radius:999px;padding:3px 10px;font-size:12px;background:var(--bg);white-space:nowrap}
button.chip{cursor:pointer;font:inherit;font-size:12px;color:var(--ink)}
.rowl{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--divider);min-width:0}
.rowl:last-child{border-bottom:0}
.rowl .nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ft{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:12px;padding-top:8px;border-top:1px solid var(--divider);font-size:11.5px;color:var(--faint);flex-wrap:wrap}
.btn{font:600 12.5px Inter,ui-sans-serif,system-ui,sans-serif;border-radius:8px;padding:7px 13px;border:1px solid var(--border);background:var(--bg);color:var(--ink);cursor:pointer}
.btn.pr{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}
.btn:disabled{opacity:.55;cursor:default}
.btn:focus-visible,button.chip:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
.msg{font-size:12.5px;margin-top:8px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
__CSS__
</style></head>
<body><div class="card" id="root"><div class="empty">Loading Birdy…</div></div>
<script>
window.BIRDY = {frontend: "__FRONTEND__", icon: "__ICON__"};
window.birdyFmt = {
  avatar() { return '<span class="av" style="background-image:url(' + BIRDY.icon + ')"></span>'; },
  money(v, cur) {
    if (v == null) return "–";
    try { return new Intl.NumberFormat(undefined, {style: "currency", currency: cur || "USD", maximumFractionDigits: v >= 100 ? 0 : 2}).format(v); }
    catch (e) { return (+v).toFixed(2); }
  },
  pct(v) { if (v == null) return ""; const r = Math.round(v); return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r) + "%"; },
  num(v, d) { if (v == null || isNaN(v)) return "–"; return (+v).toLocaleString(undefined, {maximumFractionDigits: d == null ? 2 : d}); },
  preset(p) {
    const m = {today: "Today", yesterday: "Yesterday", last_7d: "Last 7 days", last_14d: "Last 14 days", last_30d: "Last 30 days",
      this_week_mon_today: "This week", this_month: "This month", last_month: "Last month", this_quarter: "This quarter",
      last_quarter: "Last quarter", this_year: "This year", last_year: "Last year", maximum: "All time"};
    return m[p] || String(p || "").replace(/_/g, " ");
  },
  // Colour a change: for costs (CPL, CPM, CPC) up is bad; for spend it is neutral.
  chg(v, kind) {
    if (v == null) return '<em class="flat">–</em>';
    const cls = kind === "neutral" || Math.round(v) === 0 ? "flat" : (v > 0) === (kind === "cost") ? "dn" : "up";
    return '<em class="' + cls + '">' + this.pct(v) + "</em>";
  },
  esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); },
};
__BODY__
</script>
<script type="module">
import { App } from "__SDK__";
const root = document.documentElement;
const theme = (ctx) => { if (ctx && ctx.theme) root.dataset.theme = ctx.theme; };
const app = new App({name: "Birdy", version: "1.0.0"}, {}, {autoResize: true});
const ui = {
  args: {},
  open(path) { const url = BIRDY.frontend + path; app.openLink({url}).catch(() => window.open(url, "_blank")); },
  call(name, args) { return app.callServerTool({name, arguments: args || {}}); },
  tell(text) { return app.updateModelContext({content: [{type: "text", text}]}).catch(() => {}); },
};
app.ontoolinput = (p) => { ui.args = (p && p.arguments) || {}; };
app.onhostcontextchanged = theme;
app.ontoolresult = (result) => {
  const data = result && result.structuredContent;
  if (data) window.birdyRender(data, ui);
  else document.getElementById("root").innerHTML = '<div class="empty">Nothing to show.</div>';
};
await app.connect();
theme(app.getHostContext());
</script>
</body></html>
"""


def page(title: str, body: tuple[str, str], *, sdk_url: str, icon_url: str) -> str:
    """A complete card document from a (css, js) body."""
    css, js = body
    return (_SHELL.replace("__TITLE__", title).replace("__CSS__", css).replace("__BODY__", js)
            .replace("__SDK__", sdk_url).replace("__ICON__", icon_url).replace("__FRONTEND__", FRONTEND_URL))


# ── #12 All clients (check_my_accounts) ───────────────────────────────────────────────────────────────────────
ACCOUNTS = ("""
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.tile{all:unset;box-sizing:border-box;cursor:pointer;display:flex;flex-direction:column;justify-content:flex-start;text-align:left;background:var(--soft);border-radius:10px;padding:9px 10px;
  border:1px solid transparent;min-width:0}
.tile:hover{border-color:var(--pale)}
.tile.act{box-shadow:inset 3px 0 0 var(--bad)}.tile.watch{box-shadow:inset 3px 0 0 var(--warn)}
.tile .nm{font-weight:600;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .big{font-size:17px;font-weight:700;margin-top:2px}
.tile .sub{font-size:11px;color:var(--faint);margin-top:1px}
.tile .why{font-size:11px;color:var(--body);margin-top:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sec button.link{text-transform:none;letter-spacing:0;font-size:11.5px}
.sec{display:flex;align-items:center;gap:8px;margin:12px 0 6px;font-size:11px;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.05em}
.ft{display:flex;justify-content:space-between;gap:8px;margin-top:12px;padding-top:8px;border-top:1px solid var(--divider);font-size:11.5px;color:var(--faint);flex-wrap:wrap}
""", r"""
window.birdyRender = function (data, ui) {
  const F = birdyFmt, E = F.esc;
  const clients = data.clients || [], s = data.summary || {}, p = data.period || {};
  const LEVEL = {high: "act", medium: "watch", ok: "fine"};
  const tile = (c) => {
    const lvl = LEVEL[c.attention] || "fine", ch = c.change_pct || {};
    const stopped = c.prior && c.prior.spend > 0 && !c.spend;
    const cplCls = ch.cpl == null ? "flat" : ch.cpl > 0 ? "dn" : "up";
    const big = stopped ? '<span class="dn">Stopped</span>' : E(F.money(c.cpl, c.currency));
    const sub = stopped ? "no spend this period" :
      "CPL " + '<span class="' + cplCls + '">' + (F.pct(ch.cpl) || "–") + "</span> · " + (c.leads ?? "–") + " leads " +
      (ch.leads == null ? "" : '<span class="' + (ch.leads < 0 ? "dn" : "up") + '">' + F.pct(ch.leads) + "</span>");
    const why = lvl !== "fine" && c.reasons && c.reasons.length ? '<div class="why">' + E(c.reasons[0]) + "</div>" : "";
    return '<button class="tile ' + lvl + '" data-id="' + E(c.client && c.client.id) + '" title="Open ' + E(c.client && c.client.name) +
      ' in Birdy"><div class="nm">' + E(c.client && c.client.name) + '</div><div class="big n">' + big +
      '</div><div class="sub n">' + sub + "</div>" + why + "</button>";
  };
  const needs = clients.filter(c => c.attention !== "ok"), fine = clients.filter(c => c.attention === "ok");
  let showFine = needs.length === 0;
  const draw = () => {
    document.getElementById("root").innerHTML =
      '<div class="hd"><span class="av" style="background-image:url(' + BIRDY.icon + ')"></span><b>All clients</b>' +
      '<span class="pill act">Act · ' + (s.high || 0) + '</span><span class="pill watch">Watch · ' + (s.medium || 0) +
      '</span><span class="pill fine">Fine · ' + (s.ok || 0) + "</span></div>" +
      (needs.length ? '<div class="grid">' + needs.map(tile).join("") + "</div>" : '<div class="empty">Nothing needs you right now.</div>') +
      (fine.length ? '<div class="sec">Fine · ' + fine.length + ' <button class="link" id="tog">' + (showFine ? "Hide" : "Show") + "</button></div>" +
        (showFine ? '<div class="grid">' + fine.map(tile).join("") + "</div>" : "") : "") +
      '<div class="ft"><span>' + E(p.start || "") + " to " + E(p.end || "") + ' vs the period before</span><button class="link" id="all">Open in Birdy →</button></div>';
    document.querySelectorAll(".tile").forEach(t => t.onclick = () => ui.open("/clients/" + encodeURIComponent(t.dataset.id)));
    const tog = document.getElementById("tog"); if (tog) tog.onclick = () => { showFine = !showFine; draw(); };
    document.getElementById("all").onclick = () => ui.open("/clients");
  };
  draw();
};
""")


# ── #11 Client scorecard (compare_periods): one client as a scorecard, several as rows ─────────────────────────
SCORECARD = ("", r"""
window.birdyRender = function (data, ui) {
  const F = birdyFmt, E = F.esc, groups = data.groups || [];
  const head = (title) => '<div class="hd">' + F.avatar() + "<b>" + E(title) + "</b><small>" + E(F.preset(data.preset_b)) +
    " vs " + E(F.preset(data.preset_a)) + "</small></div>";
  const ch = (g, k) => ((g.changes || {})[k] || {}).pct_change;
  let html;
  if (groups.length === 1) {
    const g = groups[0], b = g.period_b || {}, cplUp = ch(g, "cpl");
    const pill = cplUp == null ? "" : cplUp <= 0 ? '<span class="pill fine">On track</span>' : cplUp >= 25 ? '<span class="pill act">Needs a look</span>' : "";
    html = head(g.group_name || "Client").replace("</b>", "</b>" + pill) + '<div class="kpis">' +
      '<div class="kpi"><i>Leads</i><strong>' + F.num(b.total_leads, 0) + "</strong>" + F.chg(ch(g, "total_leads")) + "</div>" +
      '<div class="kpi"><i>CPL</i><strong>' + E(F.money(b.cpl, g.currency)) + "</strong>" + F.chg(cplUp, "cost") + "</div>" +
      '<div class="kpi"><i>Spend</i><strong>' + E(F.money(b.spend, g.currency)) + "</strong>" + F.chg(ch(g, "spend"), "neutral") + "</div>" +
      '<div class="kpi"><i>CTR</i><strong>' + F.num(b.ctr) + "%</strong>" + F.chg(ch(g, "ctr")) + "</div></div>" +
      '<div class="ft"><span>Change vs ' + E(F.preset(data.preset_a).toLowerCase()) + '</span><button class="link" data-open="/clients/' +
      encodeURIComponent(g.group_id) + '">Open in Birdy →</button></div>';
  } else {
    const rows = groups.slice(0, 10).map(g => '<div class="rowl"><button class="link nm" data-open="/clients/' + encodeURIComponent(g.group_id) +
      '" style="color:var(--ink);font-weight:600">' + E(g.group_name) + '</button><span class="chip">Leads ' + F.chg(ch(g, "total_leads")) +
      '</span><span class="chip">CPL ' + F.chg(ch(g, "cpl"), "cost") + "</span></div>").join("");
    const more = (data.total_groups || groups.length) - Math.min(groups.length, 10);
    html = head((data.total_groups || groups.length) + " clients") + rows + '<div class="ft"><span>' + (more > 0 ? "and " + more + " more" : "Change vs " +
      E(F.preset(data.preset_a).toLowerCase())) + '</span><button class="link" data-open="/clients">Open in Birdy →</button></div>';
  }
  document.getElementById("root").innerHTML = html;
  document.querySelectorAll("[data-open]").forEach(b => b.onclick = () => ui.open(b.dataset.open));
};
""")


# ── #23 Chip strip (get_account_summary): the headline numbers in one line ─────────────────────────────────────
SUMMARY = ("""
.strip{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.strip .chip b{font-variant-numeric:tabular-nums}
.top{margin-top:8px;font-size:11.5px;color:var(--faint);display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.card{padding:10px 12px}
""", r"""
window.birdyRender = function (data, ui) {
  const F = birdyFmt, E = F.esc, o = data.overall || {}, groups = data.groups || [];
  const curs = [...new Set(groups.map(g => g.currency).filter(Boolean))], cur = curs.length === 1 ? curs[0] : null;
  const money = (v) => cur ? F.money(v, cur) : F.num(v);
  const who = groups.length === 1 ? groups[0].group_name : (data.total_groups || groups.length) + " clients";
  const leads = o.total_leads || 0, cpl = o.cpl != null ? o.cpl : (leads ? o.spend / leads : null);
  let html = '<div class="strip">' + F.avatar() + '<span class="chip"><b>' + E(who) + "</b></span>" +
    '<span class="chip">' + E(F.preset(data.preset)) + "</span>" +
    '<span class="chip">Spend <b>' + E(money(o.spend)) + "</b></span>" +
    '<span class="chip">Leads <b>' + F.num(leads, 0) + "</b></span>" +
    '<span class="chip">CPL <b>' + E(cpl == null ? "–" : money(cpl)) + "</b></span>" +
    (o.ctr != null ? '<span class="chip">CTR <b>' + F.num(o.ctr) + "%</b></span>" : "") + "</div>";
  if (groups.length > 1) {
    const top = groups.slice().sort((a, b) => (b.spend || 0) - (a.spend || 0)).slice(0, 4);
    html += '<div class="top">Biggest spend:' + top.map(g => '<button class="chip" data-open="/clients/' + encodeURIComponent(g.group_id) + '">' +
      E(g.group_name) + "</button>").join("") + (cur ? "" : " <span>(mixed currencies: totals are plain numbers)</span>") + "</div>";
  }
  document.getElementById("root").innerHTML = html;
  document.querySelectorAll("[data-open]").forEach(b => b.onclick = () => ui.open(b.dataset.open));
};
""")


# ── #13 Alert card (explain_alert, get_fired_alerts) ───────────────────────────────────────────────────────────
ALERT = ("", r"""
window.birdyRender = function (data, ui) {
  const F = birdyFmt, E = F.esc, root = document.getElementById("root");
  const ago = (t) => { if (!t) return ""; const m = Math.round((Date.now() - new Date(t)) / 60000);
    return m < 60 ? m + "m ago" : m < 1440 ? Math.round(m / 60) + "h ago" : Math.round(m / 1440) + "d ago"; };
  if (Array.isArray(data.fired)) {
    const rows = data.fired.slice(0, 8).map(f => '<div class="rowl"><span class="pill act">Fired</span><span class="nm"><b>' + E(f.alert_name) +
      "</b>" + (f.client_name ? " · " + E(f.client_name) : "") + '</span><small class="n" style="color:var(--faint)">' + E(ago(f.last_seen_at)) + "</small></div>").join("");
    root.innerHTML = '<div class="hd">' + F.avatar() + "<b>Alerts that fired</b><small>last " + E(data.days) + " days</small></div>" +
      (rows || '<div class="empty">No alerts fired in this period.</div>') +
      (data.fired.length > 8 ? '<div class="msg" style="color:var(--faint)">and ' + (data.fired.length - 8) + " more</div>" : "") +
      '<div class="ft"><span></span><button class="link" id="open">Open alerts in Birdy →</button></div>';
    document.getElementById("open").onclick = () => ui.open("/alerts");
    return;
  }
  const a = data.alert || {}, now = data.now || {}, win = a.type === "win";
  const firing = now.error ? a.status === "triggered" : !!now.triggered;
  const pill = firing ? (win ? '<span class="pill fine">Win</span>' : '<span class="pill act">Alert triggered</span>')
    : '<span class="pill" style="background:var(--soft);color:var(--faint)">Not firing now</span>';
  const clients = (now.per_client_results || []).filter(r => r.triggered).map(r => '<span class="chip">' + E(r.group_name) + "</span>").join("");
  root.innerHTML = '<div class="hd">' + pill + "<small>" + E(ago(a.last_triggered_at || a.updated_at)) + "</small></div>" +
    '<b style="font-size:16px;display:block;margin-bottom:8px">' + E(a.name) + "</b>" +
    '<div class="kpis"><div class="kpi"><i>Now</i><strong class="n ' + (firing && !win ? "dn" : firing ? "up" : "") + '">' +
    (now.error || now.current_value == null ? "–" : (+now.current_value).toFixed(Math.abs(now.current_value) >= 1000 ? 0 : 2)) + '</strong></div><div class="kpi"><i>Condition</i><strong style="font-size:13px;white-space:normal">' +
    E(data.condition_display || "") + "</strong></div></div>" +
    (clients ? '<div class="chips" style="margin-top:8px">' + clients + "</div>" : "") +
    (now.message ? '<div class="msg">' + E(now.message) + "</div>" : "") +
    '<div class="ft"><button class="btn" id="snooze">Snooze 24h</button><button class="btn pr" id="open">Open in Birdy</button></div>' +
    '<div class="msg" id="out" role="status"></div>';
  document.getElementById("open").onclick = () => ui.open("/alerts");
  const sn = document.getElementById("snooze"), out = document.getElementById("out");
  sn.onclick = async () => {
    sn.disabled = true; sn.textContent = "Snoozing…";
    try {
      const r = await ui.call("update_alert", {alert_id: a.id, snooze_hours: 24});
      const d = r.structuredContent || {};
      if (r.isError || d.error) throw new Error(d.error || (r.content && r.content[0] && r.content[0].text) || "Couldn't snooze");
      sn.textContent = "Snoozed for 24h"; out.textContent = "";
      ui.tell("The user snoozed the Birdy alert '" + a.name + "' for 24 hours from the alert card.");
    } catch (e) { sn.disabled = false; sn.textContent = "Snooze 24h"; out.innerHTML = '<span class="dn">' + E(e.message) + "</span>"; }
  };
};
""")


# ── #15 Live change confirm card (every LIVE tool) ─────────────────────────────────────────────────────────────
CONFIRM = ("""
.kv{display:grid;grid-template-columns:minmax(90px,auto) 1fr;gap:4px 12px;font-size:12.5px;margin-top:4px}
.kv dt{color:var(--faint)}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.card.live{border-color:var(--warn)}
""", r"""
window.birdyRender = function (data, ui) {
  const F = birdyFmt, E = F.esc, root = document.getElementById("root");
  const label = (k) => { const t = String(k).replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
  const val = (v) => {
    if (v == null) return "–";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    const line = (x) => { x = x || {}; const rest = Object.entries(x).filter(([k, y]) => k !== "from" && k !== "to" && y != null && typeof y !== "object")
      .slice(0, 2).map(([, y]) => E(y)).join(" · ");
      return "from" in x || "to" in x ? rest + (rest ? ": " : "") + E(x.from ?? "–") + " → " + E(x.to ?? "–") : rest; };
    if (Array.isArray(v)) return v.length && typeof v[0] !== "object" ? v.map(E).join(", ") :
      v.slice(0, 5).map(line).join("<br>") +
      (v.length > 5 ? "<br>and " + (v.length - 5) + " more" : "");
    if (typeof v === "object") return Object.entries(v).filter(([, y]) => y != null && typeof y !== "object").slice(0, 4)
      .map(([k, y]) => E(label(k)) + ": " + E(y)).join("<br>");
    return E(v);
  };
  const kv = (o) => '<dl class="kv">' + Object.entries(o || {}).filter(([k]) => !/^(confirm_code|next|needs_confirmation)$/.test(k))
    .slice(0, 12).map(([k, v]) => "<dt>" + E(label(k)) + "</dt><dd>" + val(v) + "</dd>").join("") + "</dl>";
  const done = (title, body, cls) => { root.className = "card"; root.innerHTML = '<div class="hd">' + F.avatar() +
    '<span class="pill ' + cls + '">' + E(title) + "</span></div>" + body; };
  if (!data.needs_confirmation) {
    if (data.error) return done("Not done", '<div class="msg">' + E(data.error) + "</div>", "act");
    return done("Done", kv(data), "fine");
  }
  const action = data.action || "Live change", tool = data.tool;
  root.className = "card live";
  root.innerHTML = '<div class="hd">' + F.avatar() + '<span class="pill watch">Live change · needs your OK</span></div>' +
    '<b style="font-size:15px;display:block">' + E(action) + "</b>" + kv(data.preview) +
    '<div class="ft"><span id="ttl"></span><span style="display:flex;gap:8px"><button class="btn" id="no">Cancel</button>' +
    '<button class="btn pr" id="yes">Confirm</button></span></div><div class="msg" id="out" role="status"></div>';
  const yes = document.getElementById("yes"), no = document.getElementById("no"), out = document.getElementById("out"), ttl = document.getElementById("ttl");
  let left = data.expires_in_seconds || 0;
  const tick = setInterval(() => { left -= 1; if (left <= 0) { clearInterval(tick); yes.disabled = true; ttl.textContent = "Preview expired: ask again"; }
    else ttl.textContent = "Expires in " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0"); }, 1000);
  no.onclick = () => { clearInterval(tick); done("Cancelled", '<div class="msg">Nothing changed.</div>', "watch");
    ui.tell("The user cancelled '" + action + "' in the Birdy card. Nothing changed; don't confirm it."); };
  yes.onclick = async () => {
    if (!tool) { out.textContent = "Confirm in the chat instead."; return; }
    yes.disabled = no.disabled = true; yes.textContent = "Working…";
    try {
      const r = await ui.call(tool, Object.assign({}, ui.args, {confirm_code: data.confirm_code}));
      let d = r.structuredContent;
      if (!d) { try { d = JSON.parse((r.content || [])[0].text); } catch (e) { d = {}; } }
      if (r.isError || d.error) throw new Error(d.error || ((r.content || [])[0] || {}).text || "It didn't go through.");
      clearInterval(tick); done("Done", kv(d), "fine");
      ui.tell("The user confirmed '" + action + "' in the Birdy card and it went through. Don't call the tool again for it. Result: " +
        JSON.stringify(d).slice(0, 600));
    } catch (e) {
      yes.disabled = no.disabled = false; yes.textContent = "Confirm"; out.innerHTML = '<span class="dn">' + E(e.message) + "</span>";
    }
  };
};
""")

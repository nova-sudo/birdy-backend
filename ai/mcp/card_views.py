"""
ai/mcp/card_views.py
--------------------
The HTML of Birdy's cards (see ai/mcp/cards.py). `page()` wraps a card body in the shared shell: Birdy's tokens
(light and dark, following the host's theme), the mascot in its round frame, and the SDK wiring. A body defines
`window.birdyRender(data, ui)`, called with the tool's structuredContent; `ui.open(path)` opens a Birdy page.
"""

from core.config import FRONTEND_URL

_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#FFFFFF;--soft:#F6F6F4;--ink:#18181B;--body:#52525B;--faint:#6B6B72;--border:#E7E6E3;--divider:#EFEEEB;
--primary:#5B3FD6;--tint:#EFECFB;--pale:#C3B7F2;--good:#16794C;--good-bg:#E8F4EE;--bad:#D93A40;--bad-bg:#FEF1F1;
--warn:#B4530A;--warn-bg:#FDF1E7;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1C1C1F;--soft:#242428;--ink:#F2F2F3;--body:#B9B9BF;
--faint:#8E8E95;--border:#2E2E33;--divider:#29292D;--primary:#A08CF2;--tint:#2A2546;--pale:#4A3F82;--good:#4CC38A;
--good-bg:#16291F;--bad:#FF6B70;--bad-bg:#331A1C;--warn:#F0A35E;--warn-bg:#33241A;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#1C1C1F;--soft:#242428;--ink:#F2F2F3;--body:#B9B9BF;--faint:#8E8E95;--border:#2E2E33;
--divider:#29292D;--primary:#A08CF2;--tint:#2A2546;--pale:#4A3F82;--good:#4CC38A;--good-bg:#16291F;--bad:#FF6B70;
--bad-bg:#331A1C;--warn:#F0A35E;--warn-bg:#33241A;color-scheme:dark}
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
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
__CSS__
</style></head>
<body><div class="card" id="root"><div class="empty">Loading Birdy…</div></div>
<script>
window.BIRDY = {frontend: "__FRONTEND__", icon: "__ICON__"};
window.birdyFmt = {
  money(v, cur) {
    if (v == null) return "–";
    try { return new Intl.NumberFormat(undefined, {style: "currency", currency: cur || "USD", maximumFractionDigits: v >= 100 ? 0 : 2}).format(v); }
    catch (e) { return (+v).toFixed(2); }
  },
  pct(v) { if (v == null) return ""; const r = Math.round(v); return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r) + "%"; },
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
  open(path) { const url = BIRDY.frontend + path; app.openLink({url}).catch(() => window.open(url, "_blank")); },
  call(name, args) { return app.callServerTool({name, arguments: args || {}}); },
};
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

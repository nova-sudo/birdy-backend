"""
services/tracker_script.py
--------------------------
The first-party tracking snippet a client installs on their landing pages.

Kept as a Python string rather than a static asset because the backend is
deployed to Vercel's serverless Python runtime, where reading a sibling file
off disk at request time is fragile — a module constant always ships with the
function bundle.

What the script does, in the order it matters:

  1. Establishes a durable `visitor_id` (cookie + localStorage, and adopted
     from the URL when a form/landing page hands one along) so a person keeps
     the same identity across pages, sessions and domains.
  2. Records every landing with whatever Meta/UTM identifiers are on the URL.
     `ad_id` is the identifier we actually report on — Birdy already knows
     what ad 12021988 is called, so nobody has to type ad names into UTMs.
  3. Passes the visitor_id *into* embedded forms (Typeform, ROASForm, GHL
     forms/calendars) by decorating iframe and link URLs, so a deterministic
     ID survives the form hop where the provider supports it.
  4. Falls back to identity: when a form is submitted on the page itself, it
     scrapes the email/phone and reports them, which is what lets the backend
     join the visitor to the GoHighLevel contact even when the form provider
     strips everything we sent it.

Everything is fire-and-forget (`sendBeacon`, falling back to a keepalive
`fetch` with a text/plain body). Both are CORS "simple requests", so no
preflight ever hits the API and the client never needs to read a response —
the visitor_id is minted in the browser, not handed down by the server.
"""

TRACKER_JS = r"""
(function () {
  var SITE = "__SITE_ID__";
  var API = "__ENDPOINT__";
  var VID_PARAM = "birdy_visitor_id";
  var STORE = "_birdy_vid";
  var TOUCH_KEYS = [
    "ad_id", "adset_id", "campaign_id",
    "fbclid", "gclid", "ttclid",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"
  ];
  // Form/booking providers we hand the visitor id to. Matched as a substring
  // of the URL host, so subdomains (link.msgsndr.com, form.typeform.com) hit.
  var FORM_HOSTS = [
    "typeform.com", "roasform.com", "roasform.io",
    "leadconnectorhq.com", "msgsndr.com",
    "jotform.com", "tally.so", "calendly.com", "gohighlevel.com"
  ];

  function params() {
    try { return new URLSearchParams(location.search); } catch (e) { return null; }
  }
  function param(name) {
    var p = params();
    return p ? p.get(name) : null;
  }

  // -- visitor id ----------------------------------------------------------
  function readCookie(name) {
    var m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m[2]) : null;
  }
  function writeCookie(name, value) {
    var host = location.hostname.split(".").slice(-2).join(".");
    var base = name + "=" + encodeURIComponent(value) + ";path=/;max-age=31536000;samesite=lax";
    try { document.cookie = base + ";domain=." + host; } catch (e) {}
    document.cookie = base;
  }
  function mint() {
    try {
      if (crypto && crypto.randomUUID) return "v_" + crypto.randomUUID().replace(/-/g, "");
    } catch (e) {}
    return "v_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function visitorId() {
    // An id on the URL wins: it means a page we already tracked sent this
    // person here (a redirect, or a form loaded on the provider's domain),
    // and keeping their original id is the whole point.
    var fromUrl = param(VID_PARAM);
    var stored = null;
    try { stored = localStorage.getItem(STORE); } catch (e) {}
    var id = fromUrl || stored || readCookie(STORE) || mint();
    try { localStorage.setItem(STORE, id); } catch (e) {}
    writeCookie(STORE, id);
    return id;
  }

  var VID = visitorId();

  // -- transport -----------------------------------------------------------
  function send(path, body) {
    body.site_id = SITE;
    body.visitor_id = VID;
    var url = API + path;
    var text = JSON.stringify(body);
    try {
      if (navigator.sendBeacon) {
        // text/plain keeps this a CORS simple request — no preflight.
        navigator.sendBeacon(url, new Blob([text], { type: "text/plain;charset=UTF-8" }));
        return;
      }
    } catch (e) {}
    try {
      fetch(url, {
        method: "POST",
        body: text,
        keepalive: true,
        mode: "no-cors",
        headers: { "Content-Type": "text/plain;charset=UTF-8" }
      });
    } catch (e) {}
  }

  // -- touch ---------------------------------------------------------------
  function touch() {
    var t = {};
    var p = params();
    if (p) {
      for (var i = 0; i < TOUCH_KEYS.length; i++) {
        var v = p.get(TOUCH_KEYS[i]);
        if (v) t[TOUCH_KEYS[i]] = v;
      }
    }
    t.landing_page = location.origin + location.pathname;
    t.referrer = document.referrer || null;
    send("/collect", { event: "pageview", touch: t });
  }

  // -- identity ------------------------------------------------------------
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  function looksEmail(el, val) {
    if (el.type === "email") return true;
    var hint = ((el.name || "") + " " + (el.id || "") + " " + (el.placeholder || "")).toLowerCase();
    if (/e-?mail/.test(hint)) return true;
    return EMAIL_RE.test(val);
  }
  function looksPhone(el, val) {
    if (el.type === "tel") return true;
    var hint = ((el.name || "") + " " + (el.id || "") + " " + (el.placeholder || "")).toLowerCase();
    if (/phone|mobile|tel|number/.test(hint)) return true;
    var digits = val.replace(/\D/g, "");
    return digits.length >= 7 && digits.length <= 15 && /^[\d\s+()\-.]+$/.test(val);
  }

  function identify(fields) {
    if (!fields) return;
    var email = fields.email || null;
    var phone = fields.phone || null;
    if (!email && !phone) return;
    send("/identify", { email: email, phone: phone });
  }

  function scrape(form) {
    var out = {};
    var els = form.querySelectorAll("input, textarea");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var val = (el.value || "").trim();
      if (!val || el.type === "password" || el.type === "hidden") continue;
      if (!out.email && looksEmail(el, val) && EMAIL_RE.test(val)) out.email = val;
      else if (!out.phone && looksPhone(el, val)) out.phone = val;
    }
    return out;
  }

  function stamp(form) {
    if (form.querySelector('input[name="' + VID_PARAM + '"]')) return;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = VID_PARAM;
    input.value = VID;
    form.appendChild(input);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM") return;
    try {
      stamp(form);
      identify(scrape(form));
    } catch (err) {}
  }, true);

  // -- pass the id into embedded/linked forms -------------------------------
  function isFormHost(url) {
    for (var i = 0; i < FORM_HOSTS.length; i++) {
      if (url.host.indexOf(FORM_HOSTS[i]) !== -1) return true;
    }
    return false;
  }
  function decorate(el, attr) {
    var raw = el.getAttribute(attr);
    if (!raw || raw.indexOf(VID_PARAM) !== -1) return;
    var url;
    try { url = new URL(raw, location.href); } catch (e) { return; }
    if (!isFormHost(url)) return;
    url.searchParams.set(VID_PARAM, VID);
    el.setAttribute(attr, url.toString());
  }
  function decorateAll() {
    var frames = document.querySelectorAll("iframe[src]");
    for (var i = 0; i < frames.length; i++) decorate(frames[i], "src");
    var links = document.querySelectorAll("a[href]");
    for (var j = 0; j < links.length; j++) decorate(links[j], "href");
    var forms = document.querySelectorAll("form");
    for (var k = 0; k < forms.length; k++) { try { stamp(forms[k]); } catch (e) {} }
  }

  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  ready(function () {
    decorateAll();
    // Page builders (GHL funnels, Webflow, React landing pages) mount their
    // forms after first paint, so watch for late arrivals rather than
    // decorating once and hoping.
    try {
      var pending = null;
      new MutationObserver(function () {
        if (pending) return;
        pending = setTimeout(function () { pending = null; decorateAll(); }, 250);
      }).observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
  });

  touch();

  window.birdy = window.birdy || {};
  window.birdy.visitorId = VID;
  window.birdy.identify = identify;
})();
"""


def render_tracker(site_id: str, endpoint: str) -> str:
    """Return the tracker source with this account's site id and API base baked in."""
    return TRACKER_JS.replace("__SITE_ID__", site_id).replace("__ENDPOINT__", endpoint)

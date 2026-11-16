"""A self-contained HTML page of job matches: an alternative to Notion and ntfy that needs no account.

Every run merges its open matches into a small store (`digest_matches.json`) and rewrites the page,
so matches from earlier runs stay listed (marked "earlier") for `html_digest.keep_days` days after
they were last seen. Open the page in any browser; it works offline from `file://`. Marking a match
applied or hidden is remembered in that browser only.
"""
import html
import json
import os
from datetime import datetime, timedelta, timezone

from job_search_pipeline.config import CFG, atomic_write_json, get_path, load_json
from job_search_pipeline.urls import normalize_url

_DIGEST_CFG = CFG.get("html_digest") or {}
HTML_DIGEST_ENABLED = bool(_DIGEST_CFG.get("enabled", True))
HTML_DIGEST_PATH = get_path("html_digest", "data/digest.html")
DIGEST_STORE_PATH = get_path("digest_store", "data/digest_matches.json")
HTML_DIGEST_URL = str(_DIGEST_CFG.get("url") or "")
KEEP_DAYS = int(_DIGEST_CFG.get("keep_days", 30))
ROW_LIMIT = 200

_MATCH_FIELDS = ("company", "title", "location", "posted", "score", "keyword_score", "verdict", "blockers",
                 "third_party", "review_failed", "review_deferred")


def _match_record(job, now, synced, exported):
    rec = {k: job.get(k) for k in _MATCH_FIELDS if job.get(k) not in (None, "", [])}
    rec["url"] = job.get("url")
    # keyword_reason is the pre-review text; `reason` may already carry the Notion review labels
    rec["reason"] = job.get("ai_reason") or job.get("keyword_reason") or job.get("reason") or ""
    if job.get("review_deferred"):
        rec["reason"] = "AI review deferred by a budget cap; keyword match only. " + rec["reason"]
    elif job.get("review_failed"):
        rec["reason"] = "AI review failed, retried next run; keyword match only. " + rec["reason"]
    try:
        rec["score"] = int(rec.get("score") or 0)
    except (TypeError, ValueError):
        rec["score"] = 0
    if synced:
        rec["synced"] = True
    if exported:
        rec["exported"] = True
    rec["last_seen"] = now
    return rec


def update_store(eval_results, synced_urls=(), exported_urls=(), path=None, now=None):
    """Merge this run's open matches into the store and return it (keyed by canonical URL).

    Matches not seen this run keep their last record until KEEP_DAYS after they were last seen."""
    path = path or DIGEST_STORE_PATH
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    store = load_json(path, default={})
    if not isinstance(store, dict):
        store = {}
    synced = {normalize_url(u) for u in synced_urls}
    exported = {normalize_url(u) for u in exported_urls}
    for job in eval_results.get("top_matches", []):
        if not job.get("url"):
            continue
        key = normalize_url(job["url"])
        old = store.get(key) or {}
        rec = _match_record(job, now, key in synced or old.get("synced"), key in exported or old.get("exported"))
        rec["first_seen"] = old.get("first_seen") or now
        store[key] = rec
    cutoff = (datetime.fromisoformat(now) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    store = {k: v for k, v in store.items() if isinstance(v, dict) and str(v.get("last_seen", "")) >= cutoff}
    atomic_write_json(path, store, indent=1)
    return store


def _rows(items, fields):
    return [{f: item.get(f) for f in fields if item.get(f) not in (None, "")} for item in items[:ROW_LIMIT]]


def page_data(eval_results, store, now, min_score, spend=None):
    """Everything the page renders, as one JSON-ready dict."""
    health = eval_results.get("source_health") or {}
    matches = sorted(store.values(), key=lambda r: (r.get("last_seen") == now, r.get("score", 0)), reverse=True)
    return {
        "generated": now,
        "min_score": min_score,
        "keep_days": KEEP_DAYS,
        "evaluated": eval_results.get("total_evaluated"),
        "matches": matches,
        "ai_skipped": _rows(sorted(eval_results.get("ai_skipped", []), key=lambda j: j.get("score", 0), reverse=True),
                            ("company", "title", "score", "ai_reason", "blockers", "url")),
        "location_filtered": _rows(eval_results.get("location_filtered", []),
                                   ("company", "title", "score", "phrase", "url")),
        "manual_reviews": _rows(eval_results.get("manual_reviews", []), ("company", "title", "reason", "url")),
        "drops": _rows([*eval_results.get("prefilter_drops", []), *eval_results.get("dropped_by_rule", [])],
                       ("stage", "label", "count", "examples")),
        "sources": health.get("line", ""),
        "warnings": ([eval_results["ai_review_skipped"]] if eval_results.get("ai_review_skipped") else [])
                    + list(health.get("warnings", [])),
        "spend": spend or None,
    }


def _json_for_script(data):
    """JSON safe to embed in a <script> element (no early </script>, no HTML comment openers)."""
    return (json.dumps(data, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_html(data):
    title = html.escape(str(_DIGEST_CFG.get("title") or "Job Matches"))
    return (_TEMPLATE.replace("__TITLE__", title)
            .replace("__DATA__", _json_for_script(data)))


def write_digest(eval_results, synced_urls=(), exported_urls=(), path=None, min_score=80, spend=None):
    """Update the match store and rewrite the HTML page; returns the page path."""
    path = path or HTML_DIGEST_PATH
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store = update_store(eval_results, synced_urls, exported_urls, now=now)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_html(page_data(eval_results, store, now, min_score, spend)))
    os.replace(tmp, path)
    print(f"Saved HTML digest ({len(store)} match(es)) to: {path}")
    return path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  --bg: #f6f6f3; --surface: #ffffff; --text: #1d1f21; --muted: #62666b; --line: #e2e2dc;
  --accent: #2f5bd3; --strong: #1f7a4d; --strong-bg: #e3f3ea; --mid: #8a5a00; --mid-bg: #fbf0d9;
  --low: #5d6166; --low-bg: #ececea; --warn: #9a3412; --warn-bg: #fdebe1; --chip: #f0f0ec;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15171a; --surface: #1d2024; --text: #e7e8ea; --muted: #9ba0a6; --line: #2e3237;
    --accent: #7ea2ff; --strong: #6fd19c; --strong-bg: #173526; --mid: #f2c46b; --mid-bg: #3a2f17;
    --low: #b4b8bd; --low-bg: #2a2d31; --warn: #ffab85; --warn-bg: #3d2117; --chip: #272a2f;
  }
}
:root[data-theme="dark"] {
  --bg: #15171a; --surface: #1d2024; --text: #e7e8ea; --muted: #9ba0a6; --line: #2e3237;
  --accent: #7ea2ff; --strong: #6fd19c; --strong-bg: #173526; --mid: #f2c46b; --mid-bg: #3a2f17;
  --low: #b4b8bd; --low-bg: #2a2d31; --warn: #ffab85; --warn-bg: #3d2117; --chip: #272a2f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 920px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 32px 0 12px; display: flex; gap: 8px; align-items: baseline; }
h2 small { color: var(--muted); font-weight: 400; font-size: 14px; }
a { color: var(--accent); }
.meta { color: var(--muted); font-size: 14px; }
.stats { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; min-width: 110px; }
.stat b { display: block; font-size: 20px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 13px; }
.warn { background: var(--warn-bg); color: var(--warn); border-radius: 10px; padding: 10px 12px; margin: 8px 0; font-size: 14px; }
.controls { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 10px 0; display: flex; flex-wrap: wrap;
  gap: 8px 16px; align-items: center; border-bottom: 1px solid var(--line); }
.controls input[type=search] { flex: 1 1 220px; min-width: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--surface); color: var(--text); font: inherit; }
.controls select { padding: 7px 8px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text); font: inherit; }
.controls label { font-size: 14px; color: var(--muted); display: flex; gap: 6px; align-items: center; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin: 10px 0;
  display: grid; grid-template-columns: 56px 1fr; gap: 4px 14px; }
.card.applied { opacity: .6; }
.score { grid-row: span 3; width: 56px; height: 56px; border-radius: 12px; display: grid; place-items: center;
  font-weight: 700; font-size: 17px; font-variant-numeric: tabular-nums; }
.s-strong { background: var(--strong-bg); color: var(--strong); }
.s-mid { background: var(--mid-bg); color: var(--mid); }
.s-low { background: var(--low-bg); color: var(--low); }
.head, .sub, .reason { grid-column: 2; min-width: 0; }
.head { font-weight: 600; overflow-wrap: anywhere; }
.head .co { color: var(--muted); font-weight: 500; }
.sub { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 4px 10px; }
.reason { font-size: 14px; overflow-wrap: anywhere; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.chip { background: var(--chip); border-radius: 999px; padding: 1px 9px; font-size: 12px; }
.chip.gap { background: var(--warn-bg); color: var(--warn); }
.badge { border: 1px solid var(--line); border-radius: 6px; padding: 0 6px; font-size: 12px; }
.actions { grid-column: 2; display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.actions a, .actions button { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 7px; border: 1px solid var(--line);
  background: transparent; color: var(--text); cursor: pointer; text-decoration: none; }
.actions a { background: var(--accent); border-color: var(--accent); color: var(--surface); }
.actions button[aria-pressed=true] { background: var(--chip); font-weight: 600; }
.empty { color: var(--muted); font-size: 14px; padding: 8px 0; }
details { margin: 12px 0; background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 14px; }
summary { cursor: pointer; font-weight: 600; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.tbl td, .tbl th { text-align: left; padding: 6px 6px; border-top: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; }
.tbl th { color: var(--muted); font-weight: 500; }
.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
@media (max-width: 560px) {
  .card { grid-template-columns: 44px 1fr; gap: 4px 10px; padding: 12px; }
  .score { width: 44px; height: 44px; font-size: 15px; grid-row: span 2; }
  .reason, .actions { grid-column: 1 / -1; }
  .tbl .hide-sm { display: none; }
}
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <div class="meta" id="meta"></div>
  <div id="warnings"></div>
  <div class="stats" id="stats"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search company, title, reason…" aria-label="Search matches">
    <label>Min match <select id="min"><option value="0">Any</option><option value="50">50%+</option>
      <option value="65">65%+</option><option value="80">80%+</option><option value="90">90%+</option></select></label>
    <label><input type="checkbox" id="no3p"> Hide third-party</label>
    <label><input type="checkbox" id="showHidden"> Show hidden</label>
  </div>
  <section id="lists"></section>
  <section id="extra"></section>
  <p class="meta" id="foot"></p>
</main>
<script id="digest-data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("digest-data").textContent);
  var KEY = "job-digest-marks-v1";
  var marks = {};
  try { marks = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { marks = {}; }
  function saveMarks() { try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) {} }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "cls") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function safeUrl(u) { return /^https?:\/\//i.test(u || "") ? u : null; }
  function link(u, text) { var s = safeUrl(u); return s ? el("a", {href: s, target: "_blank", rel: "noopener noreferrer", text: text}) : el("span", {text: text}); }
  function when(iso) {
    var d = new Date(iso); if (isNaN(d)) return iso || "";
    return d.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
  }
  function title(s) { return String(s || "Unknown").replace(/\b\w/g, function (c) { return c.toUpperCase(); }); }
  function tier(s) { return s >= D.min_score ? "s-strong" : s >= 65 ? "s-mid" : "s-low"; }

  document.getElementById("meta").textContent = "Updated " + when(D.generated) +
    (D.evaluated ? " · " + D.evaluated + " postings evaluated this run" : "");
  D.warnings.forEach(function (w) { document.getElementById("warnings").appendChild(el("div", {cls: "warn", text: "⚠ " + w})); });

  var current = D.matches.filter(function (m) { return m.last_seen === D.generated; });
  var fresh = current.filter(function (m) { return m.first_seen === D.generated; });
  var atBar = current.filter(function (m) { return m.score >= D.min_score && !m.review_failed; });
  var stats = [[fresh.length, "new this run"], [current.length, "open matches"], [atBar.length, "at " + D.min_score + "%+"]];
  if (D.spend) {
    var s = D.spend, cost = (s.usd_unknown && !s.usd) ? (s.tokens || 0).toLocaleString() + " tok"
      : (s.usd_estimated || s.usd_unknown ? "~" : "") + "$" + (s.usd || 0).toFixed(2);
    stats.push([cost, "AI review this run"]);
    var today = (s.usd_unknown && !s.today_usd) ? (s.today_tokens || 0).toLocaleString() + " tok" : "$" + (s.today_usd || 0).toFixed(2);
    stats.push([today, "AI review today"]);
    stats.push([(s.cache_hits || 0) + (s.repost_hits || 0), "reviews reused"]);
  }
  stats.forEach(function (p) { document.getElementById("stats").appendChild(el("div", {cls: "stat"}, [el("b", {text: String(p[0])}), el("span", {text: p[1]})])); });

  function card(m) {
    var mark = marks[m.url] || "";
    var sub = el("div", {cls: "sub"}, [
      m.location ? el("span", {text: m.location}) : null,
      m.posted ? el("span", {text: "Posted " + m.posted}) : null,
      el("span", {text: "First seen " + when(m.first_seen)}),
      m.keyword_score != null ? el("span", {text: "Keyword " + m.keyword_score + "%"}) : null,
      m.verdict ? el("span", {cls: "badge", text: m.verdict.toUpperCase()}) : null,
      m.third_party ? el("span", {cls: "badge", text: "Third-party poster"}) : null,
      m.synced ? el("span", {cls: "badge", text: "In Notion"}) : null,
      m.review_failed ? el("span", {cls: "badge", text: m.review_deferred ? "Review deferred" : "Review failed"}) : null
    ]);
    var chips = (m.blockers || []).length ? el("div", {cls: "chips"}, m.blockers.map(function (b) { return el("span", {cls: "chip gap", text: b}); })) : null;
    function toggle(kind, label) {
      var b = el("button", {type: "button", "aria-pressed": String(mark === kind), text: label});
      b.addEventListener("click", function () { if (marks[m.url] === kind) delete marks[m.url]; else marks[m.url] = kind; saveMarks(); render(); });
      return b;
    }
    var actions = el("div", {cls: "actions"}, [safeUrl(m.url) ? link(m.url, "Open posting ↗") : null,
      toggle("applied", mark === "applied" ? "Applied ✓" : "Mark applied"), toggle("hidden", mark === "hidden" ? "Unhide" : "Hide")]);
    return el("article", {cls: "card" + (mark === "applied" ? " applied" : "")}, [
      el("div", {cls: "score " + tier(m.score), text: m.score + "%", title: "Match score"}),
      el("div", {cls: "head"}, [el("span", {cls: "co", text: title(m.company) + " · "}), m.title || "Untitled role"]),
      sub,
      el("div", {cls: "reason"}, [m.reason || "", chips]),
      actions
    ]);
  }

  function visible(m) {
    var q = document.getElementById("q").value.trim().toLowerCase();
    var min = +document.getElementById("min").value;
    if (m.score < min) return false;
    if (document.getElementById("no3p").checked && m.third_party) return false;
    if (marks[m.url] === "hidden" && !document.getElementById("showHidden").checked) return false;
    if (q) {
      var hay = [m.company, m.title, m.location, m.reason, (m.blockers || []).join(" ")].join(" ").toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    return true;
  }

  function section(host, label, note, items) {
    var shown = items.filter(visible).sort(function (a, b) {
      return ((marks[a.url] === "applied") - (marks[b.url] === "applied")) || (b.score - a.score);
    });
    host.appendChild(el("h2", null, [label, el("small", {text: shown.length + (shown.length !== items.length ? " of " + items.length : "") + (note ? " · " + note : "")})]));
    if (!shown.length) host.appendChild(el("div", {cls: "empty", text: items.length ? "Nothing matches the filters." : "None."}));
    shown.forEach(function (m) { host.appendChild(card(m)); });
  }

  function render() {
    var host = document.getElementById("lists"); host.textContent = "";
    var openOld = current.filter(function (m) { return m.first_seen !== D.generated; });
    var earlier = D.matches.filter(function (m) { return m.last_seen !== D.generated; });
    section(host, "New this run", "", fresh);
    section(host, "Still open", "seen in earlier runs and again now", openOld);
    section(host, "Earlier", "not in this run: closed, reposted, or already sent on; kept " + D.keep_days + " days", earlier);
  }

  function table(label, rows, cols, total) {
    if (!rows.length) return;
    var head = el("tr", null, cols.map(function (c) { return el("th", {cls: c.cls || "", text: c.label}); }));
    var body = rows.map(function (r) { return el("tr", null, cols.map(function (c) { return el("td", {cls: c.cls || ""}, [c.cell(r)]); })); });
    document.getElementById("extra").appendChild(el("details", null, [el("summary", {text: label + " (" + (total != null ? total : rows.length) + ")"}),
      el("table", {cls: "tbl"}, [el("thead", null, [head]), el("tbody", null, body)])]));
  }
  var role = {label: "Role", cell: function (r) { return link(r.url, title(r.company) + " · " + (r.title || "")); }};
  var pct = {label: "%", cls: "num", cell: function (r) { return document.createTextNode(r.score != null ? r.score + "%" : ""); }};
  table("Filtered out by AI review this run", D.ai_skipped, [role, pct,
    {label: "Why", cls: "hide-sm", cell: function (r) { return document.createTextNode((r.ai_reason || "") + ((r.blockers || []).length ? " [" + r.blockers.join("; ") + "]" : "")); }}]);
  table("Dropped for onsite / hybrid / location wording this run", D.location_filtered, [role, pct,
    {label: "Phrase", cls: "hide-sm", cell: function (r) { return document.createTextNode(r.phrase || ""); }}]);
  table("Manual review needed (description could not be fetched)", D.manual_reviews, [role,
    {label: "Reason", cls: "hide-sm", cell: function (r) { return document.createTextNode(r.reason || ""); }}]);
  function txt(key) { return function (r) { return document.createTextNode(r[key] != null ? String(r[key]) : ""); }; }
  table("Dropped by deterministic rules", D.drops || [], [
    {label: "Stage", cls: "hide-sm", cell: txt("stage")}, {label: "Rule", cell: txt("label")},
    {label: "Count", cls: "num", cell: txt("count")},
    {label: "Example", cls: "hide-sm", cell: function (r) {
      var ex = (r.examples || [])[0];
      return ex ? el("span", null, [link(ex.url, title(ex.company) + " · " + (ex.title || "")), ": " + (ex.detail || "")])
        : document.createTextNode("");
    }}], (D.drops || []).reduce(function (n, r) { return n + (r.count || 0); }, 0));

  var foot = [];
  if (D.sources) foot.push(D.sources);
  if (D.spend && D.spend.caps && Object.keys(D.spend.caps).length) {
    foot.push("AI review caps: " + Object.keys(D.spend.caps).map(function (k) { return D.spend.caps[k] + " " + k; }).join(", ") +
      (D.spend.deferred ? " · " + D.spend.deferred + " deferred by the " + D.spend.stopped_by + " cap" : ""));
  }
  foot.push("Applied / hidden marks are saved in this browser only.");
  document.getElementById("foot").textContent = foot.join(" · ");

  ["q", "min", "no3p", "showHidden"].forEach(function (id) { document.getElementById(id).addEventListener("input", render); });
  render();
})();
</script>
</body>
</html>
"""

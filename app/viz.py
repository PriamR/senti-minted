"""Render a timeline dict to a self-contained HTML chart (inline SVG, no CDN).

`render_html(timeline)` is the static artifact the CLI writes. With `ask=True`
(served by app/api.py) the page also carries a question box wired to /api/ask.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

W, H = 1040, 440
PAD_L, PAD_R, PAD_T, PAD_B = 56, 24, 24, 40
PLOT_W = W - PAD_L - PAD_R
PLOT_H = H - PAD_T - PAD_B

ASPECT_LABELS = {
    "ui_controls": "UI & controls", "developer": "developer & updates",
    "expectations": "promises & hype", "value": "price & value",
}


def _x(ts: int, t0: int, t1: int) -> float:
    if t1 <= t0:
        return PAD_L
    return PAD_L + (ts - t0) / (t1 - t0) * PLOT_W


def _y(v: float) -> float:
    return PAD_T + (1 - v) * PLOT_H  # approval 0..1, 1 at top


def _pct(v: Any, signed: bool = False) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v * 100:+.1f} pts" if signed else f"{v:.0%}"


def _aspect(key: str) -> str:
    return ASPECT_LABELS.get(key, key.replace("_", " "))


def _svg(timeline: dict[str, Any]) -> str:
    base = timeline["baseline"]
    pts = [p for p in base["points"] if p["approval"] is not None]
    if not pts:
        return '<p>No review history.</p>'
    t0 = base.get("start_ts") or pts[0]["ts"]
    t1 = base.get("end_ts") or pts[-1]["ts"]

    parts: list[str] = [f'<svg viewBox="0 0 {W} {H}" class="chart" xmlns="http://www.w3.org/2000/svg" '
                        f'role="img" aria-label="Monthly approval over time">']

    # gridlines + y labels (0,25,50,75,100%)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = _y(frac)
        cls = "grid mid" if frac == 0.5 else "grid"
        parts.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" class="{cls}"/>')
        parts.append(f'<text x="{PAD_L-8}" y="{y+4:.1f}" class="ylab">{int(frac*100)}%</text>')

    # year ticks
    y0 = datetime.fromtimestamp(t0, tz=timezone.utc).year
    y1 = datetime.fromtimestamp(t1, tz=timezone.utc).year
    for yr in range(y0, y1 + 1):
        ts = int(datetime(yr, 1, 1, tzinfo=timezone.utc).timestamp())
        if ts < t0 or ts > t1:
            continue
        x = _x(ts, t0, t1)
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H-PAD_B}" class="grid faint"/>')
        parts.append(f'<text x="{x:.1f}" y="{H-PAD_B+18}" class="xlab">{yr}</text>')

    # analyzed window bands (drawn under everything else)
    for ev in timeline["events"]:
        if not ev.get("analysis"):
            continue
        w = ev["window"]
        xs = _x(max(w["start_ts"], t0), t0, t1)
        xe = _x(min(w["end_ts"], t1), t0, t1)
        if xe > xs:
            parts.append(f'<rect x="{xs:.1f}" y="{PAD_T}" width="{max(xe-xs, 2):.1f}" height="{PLOT_H}" class="band"/>')

    # approval polyline + month points with tooltips
    poly = " ".join(f"{_x(p['ts'], t0, t1):.1f},{_y(p['approval']):.1f}" for p in pts)
    parts.append(f'<polyline points="{poly}" class="curve"/>')
    for p in pts:
        parts.append(
            f'<circle cx="{_x(p["ts"], t0, t1):.1f}" cy="{_y(p["approval"]):.1f}" r="5" class="hit">'
            f'<title>{p["date"][:7]} · {p["approval"]:.0%} approval · {p["n"]:,} reviews</title></circle>'
        )

    # event ticks along the bottom: colour = significant before/after move
    for ev in timeline["events"]:
        if ev["ts"] < t0 - 30 * 86400 or ev["ts"] > t1:
            continue
        x = _x(max(ev["ts"], t0), t0, t1)
        m = ev.get("movement") or {}
        cls = "tick"
        if m.get("significant"):
            cls += " up" if (m.get("delta") or 0) > 0 else " down"
        tip = f'{ev["date"]} · {ev["title"]}'
        if m.get("delta") is not None:
            tip += f' · before→after {_pct(m["delta"], True)} (z={m["z"]}, n={m["n_before"]}/{m["n_after"]})'
        parts.append(f'<line x1="{x:.1f}" y1="{H-PAD_B-10}" x2="{x:.1f}" y2="{H-PAD_B}" class="{cls}">'
                     f'<title>{html.escape(tip)}</title></line>')

    # analyzed markers
    for ev in (e for e in timeline["events"] if e.get("analysis")):
        x = _x(max(ev["ts"], t0), t0, t1)
        a = ev["analysis"]
        tip = f'{ev["date"]} · {ev["title"]} · text approval {_pct(a.get("approval_text"))}'
        if a.get("summary"):
            tip += f' · {a["summary"]}'
        parts.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H-PAD_B}" class="ev analyzed">'
                     f'<title>{html.escape(tip)}</title></line>')
        parts.append(f'<a href="#ev-{ev["ts"]}"><circle cx="{x:.1f}" cy="{PAD_T+8}" r="5" class="evdot">'
                     f'<title>{html.escape(tip)}</title></circle></a>')

    parts.append("</svg>")
    return "".join(parts)


def _aspect_bars(a: dict[str, Any], limit: int = 6) -> str:
    rows = [x for x in a.get("aspects", []) if x["aspect"] != "other"][:limit]
    if not rows:
        return ""
    top = max(x["mentions"] for x in rows)
    out = []
    for x in rows:
        sided = x["positive"] + x["negative"]
        pos_w = 100 * x["positive"] / top
        neg_w = 100 * x["negative"] / top
        neu_w = 100 * x["neutral"] / top
        details = ", ".join(f'{d["detail"]} ({"+" if d["sentiment"] == "positive" else "−" if d["sentiment"] == "negative" else "·"})'
                            for d in x.get("top_details", [])[:3])
        out.append(
            f'<div class="arow" title="{html.escape(details)}">'
            f'<span class="aname">{html.escape(_aspect(x["aspect"]))}</span>'
            f'<span class="abar"><i class="p" style="width:{pos_w:.0f}%"></i>'
            f'<i class="n" style="width:{neg_w:.0f}%"></i><i class="z" style="width:{neu_w:.0f}%"></i></span>'
            f'<span class="anum">{x["mentions"]} · {_pct(x["positive_share"]) if sided else "—"}</span></div>'
        )
    return f'<div class="aspects">{"".join(out)}</div>'


def _event_cards(timeline: dict[str, Any]) -> str:
    cards = []
    analyzed = sorted((e for e in timeline["events"] if e.get("analysis")), key=lambda e: e["ts"])
    for ev in analyzed:
        a = ev["analysis"]
        delta = a.get("delta")
        delta_html = "—"
        if isinstance(delta, (int, float)):
            cls = "up" if delta >= 0 else "down"
            z = (ev.get("movement") or {}).get("z")
            delta_html = f'<span class="delta {cls}">{_pct(delta, True)}</span> <span class="muted">z={z}</span>'
        drivers = "".join(
            f'<li class="{html.escape(d["direction"])}"><b>{html.escape(_aspect(d["aspect"]))}</b>'
            f'<span class="w">{d["mentions"]} mentions · {_pct(d.get("positive_share"))} positive</span>'
            f'<span class="why">{html.escape(d.get("why") or "")}</span>'
            + "".join(f'<q>{html.escape(q)}</q>' for q in d.get("evidence", [])[:1])
            + "</li>"
            for d in a.get("drivers", [])
        )
        w = ev.get("window", {})
        span = f'{w.get("start_date","?")} → {w.get("end_date","?")}'
        merged = f' · {ev["n_events"]} posts' if ev.get("n_events", 1) > 1 else ""
        trunc = ""
        if a.get("window_truncated"):
            trunc = (f' <span class="warn">(after-window only {a.get("after_days_observed","?")}d '
                     f'of {w.get("after_days","?")}d so far)</span>')
        s = a.get("sample", {})
        method = a.get("method", "")
        badge = f'<span class="badge {"warn-b" if a.get("degraded") else ""}">{html.escape(method)}' \
                f'{" · degraded" if a.get("degraded") else ""}</span>'
        tension = f'<p class="tension">Split: {html.escape(a["tension"])}</p>' if a.get("tension") else ""
        flags = ""
        if a.get("ungrounded_numbers"):
            flags = f'<p class="warn">Unverified figures in prose: {html.escape(", ".join(a["ungrounded_numbers"]))}</p>'
        cards.append(f"""
        <div class="card" id="ev-{ev['ts']}">
          <div class="card-head">
            <span class="badge {html.escape(ev['type'])}">{html.escape(ev['type'])}</span>
            <span class="ev-title">{html.escape(ev['title'])}</span>
            <span class="ev-date">{ev['date']}</span>
          </div>
          <div class="span">{span}{merged}{trunc} · <i>{html.escape(ev.get('selection_reason') or '')}</i></div>
          <div class="metrics">
            <span>Approval (all reviews): <b>{_pct(a.get('approval_population'))}</b> of {a.get('n_population', 0):,}</span>
            <span>Before→after: {delta_html}</span>
            <span>Text approval: <b>{_pct(a.get('approval_text'))}</b></span>
            <span>Divisiveness: <b>{_pct(a.get('divisiveness'))}</b></span>
            <span>Vote≠text: <b>{_pct(a.get('vote_text_mismatch'))}</b></span>
          </div>
          <p class="summary">{html.escape(a.get('summary') or '')}</p>
          {tension}{flags}
          <ul class="drivers">{drivers}</ul>
          {_aspect_bars(a)}
          <div class="foot">{badge} n={a.get('n_reviews', 0)} sampled from {s.get('population_n', 0):,}
            ({s.get('language','')}, {s.get('strata','?')} strata) · {_pct(s.get('edited_later_share'))} of
            fetched reviews excluded as edited after the window · ${a.get('usage', {}).get('cost_usd', 0):.4f}</div>
        </div>""")
    return "\n".join(cards) or '<p class="muted">No event windows were analyzed.</p>'


_ASK = """
<section class="ask">
  <form id="ask-form">
    <input id="ask-q" type="text" autocomplete="off" placeholder="Ask about this game, e.g. what drove reception after launch?">
    <button type="submit">Ask</button>
  </form>
  <div id="ask-out" aria-live="polite"></div>
</section>
<script src="/static/md.js"></script>
<script>
const NAME = __NAME__;
document.getElementById('ask-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const q = document.getElementById('ask-q').value.trim();
  if (!q) return;
  const out = document.getElementById('ask-out');
  const question = q.toLowerCase().includes(NAME.toLowerCase()) ? q : `${q} (about ${NAME})`;
  out.innerHTML = '<p class="muted">Thinking — the agent is calling tools…</p>';
  try {
    const r = await fetch('/api/ask', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({question})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const trace = d.provenance.map(p => `<li><code>${esc(p.tool)}(${esc(JSON.stringify(p.args))})</code></li>`).join('');
    const cost = d.usage ? ` · $${d.usage.cost_usd.toFixed(4)}` : '';
    out.innerHTML = `<div class="answer">${md(d.answer)}</div>
      <details><summary>${d.provenance.length} tool call(s)${cost}</summary><ul>${trace}</ul></details>`;
  } catch (err) {
    out.innerHTML = `<p class="warn">Error: ${String(err.message || err)}</p>`;
  }
});
</script>
"""

_CSS = """
  :root { --bg:#0f1117; --panel:#171a23; --line:#2a2f3a; --text:#e6e8ee; --muted:#8b90a0;
          --curve:#5b9dff; --up:#46c98b; --down:#ef6f6f; --ev:#3a3f4c; --evon:#f4b740; }
  * { box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font:14px/1.5 system-ui,sans-serif; margin:0; padding:28px; }
  a { color:var(--curve); }
  h1 { font-size:20px; margin:0 0 2px; } .sub { color:var(--muted); margin:0 0 18px; max-width:900px; }
  .chart { width:100%; height:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; }
  .grid { stroke:var(--line); stroke-width:1; } .grid.faint { stroke:#20242e; }
  .grid.mid { stroke:#3a4150; stroke-dasharray:4 4; }
  .ylab { fill:var(--muted); font-size:11px; text-anchor:end; }
  .xlab { fill:var(--muted); font-size:11px; text-anchor:middle; }
  .curve { fill:none; stroke:var(--curve); stroke-width:2; }
  .hit { fill:transparent; } .hit:hover { fill:var(--curve); }
  .band { fill:var(--evon); opacity:0.09; }
  .tick { stroke:var(--ev); stroke-width:1.5; } .tick.up { stroke:var(--up); } .tick.down { stroke:var(--down); }
  .ev.analyzed { stroke:var(--evon); stroke-width:1.2; stroke-dasharray:3 3; }
  .evdot { fill:var(--evon); cursor:pointer; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:12px; margin:8px 2px 0; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:12px; margin-top:18px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; scroll-margin-top:12px;
          min-width:0; overflow-wrap:anywhere; }
  .card:target { border-color:var(--evon); }
  .card-head { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
  .ev-title { font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ev-date { color:var(--muted); font-size:12px; }
  .span { color:var(--muted); font-size:12px; margin:0 0 8px; }
  .warn { color:var(--evon); }
  .badge { font-size:10px; text-transform:uppercase; padding:2px 6px; border-radius:5px; background:#222736; color:var(--muted); }
  .badge.patch { background:#1c3a2c; color:var(--up); } .badge.launch { background:#3a2c1c; color:var(--evon); }
  .badge.warn-b { background:#3a2c1c; color:var(--evon); }
  .metrics { display:flex; flex-wrap:wrap; gap:6px 16px; font-size:12px; color:var(--muted); }
  .metrics b { color:var(--text); }
  .delta { padding:1px 6px; border-radius:5px; font-weight:600; }
  .delta.up { background:#13301f; color:var(--up); } .delta.down { background:#311717; color:var(--down); }
  .summary { margin:8px 0 6px; } .tension { margin:0 0 6px; color:var(--muted); font-size:13px; }
  .drivers { list-style:none; padding:0; margin:0 0 8px; display:grid; gap:6px; }
  .drivers li { font-size:12.5px; padding:6px 8px; border-radius:8px; background:#1d212c; border-left:3px solid var(--ev); }
  .drivers li.positive { border-left-color:var(--up); } .drivers li.negative { border-left-color:var(--down); }
  .drivers .w { color:var(--muted); margin-left:8px; font-size:11.5px; }
  .drivers .why { display:block; margin-top:2px; }
  .drivers q { display:block; color:var(--muted); font-style:italic; margin-top:3px; }
  .aspects { display:grid; gap:3px; margin:6px 0 8px; }
  .arow { display:grid; grid-template-columns:118px 1fr 64px; gap:8px; align-items:center; font-size:11.5px; }
  .aname { color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .abar { display:flex; height:8px; background:#20242e; border-radius:4px; overflow:hidden; }
  .abar i { display:block; height:100%; } .abar .p { background:var(--up); } .abar .n { background:var(--down); }
  .abar .z { background:#4a5060; }
  .anum { color:var(--muted); text-align:right; }
  .foot { color:var(--muted); font-size:11px; border-top:1px solid var(--line); padding-top:6px; }
  .muted { color:var(--muted); }
  .ask { margin:18px 0 0; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; }
  .ask form { display:flex; gap:8px; }
  .ask input { flex:1; background:#0f1117; color:var(--text); border:1px solid var(--line); border-radius:8px; padding:9px 10px; font:inherit; }
  .ask button, .btn { background:var(--curve); color:#0b0d12; border:0; border-radius:8px; padding:9px 16px; font-weight:600; cursor:pointer; }
  .answer { margin-top:10px; white-space:normal; max-width:900px; } .answer p { margin:0 0 8px; }
  .answer ul { margin:0 0 8px; padding-left:20px; }
  table.md { border-collapse:collapse; margin:4px 0 10px; font-size:13px; }
  table.md th, table.md td { border-bottom:1px solid var(--line); padding:4px 10px 4px 0; text-align:left; }
  table.md th { color:var(--muted); font-weight:500; }
  details { margin-top:8px; color:var(--muted); font-size:12px; } code { font-size:11.5px; }
  .topbar { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
  .meta { color:var(--muted); font-size:12px; margin-top:14px; }
  @media (max-width:600px) { body { padding:16px; } .cards { grid-template-columns:minmax(0, 1fr); }
    .arow { grid-template-columns:96px 1fr 58px; } .ask form { flex-wrap:wrap; } }
"""


def render_html(timeline: dict[str, Any], *, ask: bool = False) -> str:
    win = timeline.get("window", {})
    u = timeline.get("llm_usage") or {}
    c = timeline.get("cache") or {}
    sel = timeline.get("selection") or {}
    pts = [p for p in timeline["baseline"]["points"] if p["approval"] is not None]
    arc = ""
    if pts:
        arc = f'{pts[0]["date"][:7]} {pts[0]["approval"]:.0%} → {pts[-1]["date"][:7]} {pts[-1]["approval"]:.0%}. '
    chosen = {
        "movers": f"the launch plus the {max(0, sel.get('k', 0) - 1)} biggest significant movers",
        "recent": f"the {sel.get('k', 0)} most recent event groups",
        "all": "every event group",
    }.get(sel.get("strategy"), "selected event groups")
    home = '<a href="/">← all games</a>' if ask else ""
    payload = json.dumps(timeline, ensure_ascii=False).replace("</", "<\\/")  # no early </script>
    ask_html = _ASK.replace("__NAME__", json.dumps(timeline["name"])) if ask else ""
    degraded = ('<p class="warn">Some windows ran in degraded mode (rule-based sentiment) because the LLM '
                'was unavailable.</p>') if timeline.get("degraded") else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(timeline['name'])} — Sentiment Journey</title>
<style>{_CSS}</style></head>
<body>
  <div class="topbar"><h1>{html.escape(timeline['name'])} — Sentiment Journey</h1>{home}</div>
  <p class="sub">Monthly approval from Steam's review histogram: {arc}Ticks along the bottom are the
     developer's announcements, grouped when within {win.get('chain_gap_days', 7)} days (green/red = approval
     moved significantly from the {win.get('before_days', 7)} days before to the {win.get('after_days', 21)} days
     after). Gold windows were sampled and read for aspects and drivers: {chosen}.</p>
  {degraded}
  {_svg(timeline)}
  <div class="legend"><span><i style="background:var(--curve)"></i>monthly approval</span>
    <span><i style="background:var(--up)"></i>significant rise</span>
    <span><i style="background:var(--down)"></i>significant fall</span>
    <span><i style="background:var(--evon)"></i>analyzed window</span></div>
  {ask_html}
  <div class="cards">{_event_cards(timeline)}</div>
  <p class="meta">Generated {timeline['generated_at'][:16].replace('T', ' ')} UTC · method {html.escape(str(timeline.get('method', '')))}
    · LLM {u.get('calls', 0)} calls, ${u.get('cost_usd', 0):.4f} (prices as of {u.get('prices_as_of', '?')})
    · cache hit rate {c.get('hit_rate')}</p>
  <script>window.__TIMELINE__ = {payload};</script>
</body></html>"""

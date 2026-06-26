"""Render a timeline dict to a self-contained HTML chart (inline SVG, no CDN)."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

W, H = 1040, 440
PAD_L, PAD_R, PAD_T, PAD_B = 56, 24, 24, 40
PLOT_W = W - PAD_L - PAD_R
PLOT_H = H - PAD_T - PAD_B


def _x(ts: int, t0: int, t1: int) -> float:
    if t1 <= t0:
        return PAD_L
    return PAD_L + (ts - t0) / (t1 - t0) * PLOT_W


def _y(v: float) -> float:
    return PAD_T + (1 - v) * PLOT_H  # approval 0..1, 1 at top


def _svg(timeline: dict[str, Any]) -> str:
    base = timeline["baseline"]
    pts = [p for p in base["points"] if p["approval"] is not None]
    if not pts:
        return '<p>No review history.</p>'
    t0 = base.get("start_ts") or pts[0]["ts"]
    t1 = base.get("end_ts") or pts[-1]["ts"]

    parts: list[str] = [f'<svg viewBox="0 0 {W} {H}" class="chart" xmlns="http://www.w3.org/2000/svg">']

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

    # approval polyline
    poly = " ".join(f"{_x(p['ts'], t0, t1):.1f},{_y(p['approval']):.1f}" for p in pts)
    parts.append(f'<polyline points="{poly}" class="curve"/>')

    # event markers
    for ev in timeline["events"]:
        if ev["ts"] < t0 or ev["ts"] > t1:
            continue
        x = _x(ev["ts"], t0, t1)
        analyzed = ev.get("analysis") is not None
        cls = "ev analyzed" if analyzed else "ev"
        tip = f'{ev["date"]} · {ev["title"]}'
        if analyzed:
            a = ev["analysis"]
            llm = a.get("approval_llm")
            tip += f' — approval(LLM) {llm:.0%}' if isinstance(llm, (int, float)) else ""
            if a.get("summary"):
                tip += f' · {a["summary"]}'
        parts.append(
            f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{H-PAD_B}" class="{cls}">'
            f'<title>{html.escape(tip)}</title></line>'
        )
        if analyzed:
            parts.append(f'<circle cx="{x:.1f}" cy="{PAD_T+6}" r="4" class="evdot"/>')

    parts.append("</svg>")
    return "".join(parts)


def _event_cards(timeline: dict[str, Any]) -> str:
    cards = []
    for ev in timeline["events"]:
        a = ev.get("analysis")
        if not a:
            continue
        delta = a.get("delta")
        delta_html = ""
        if isinstance(delta, (int, float)):
            sign = "+" if delta >= 0 else ""
            cls = "up" if delta >= 0 else "down"
            delta_html = f'<span class="delta {cls}">{sign}{delta*100:.0f} pts</span>'
        llm = a.get("approval_llm")
        llm_html = f'{llm:.0%}' if isinstance(llm, (int, float)) else "—"
        div = a.get("divisiveness")
        div_html = f'{div:.0%}' if isinstance(div, (int, float)) else "—"
        drivers = "".join(
            f'<li class="{html.escape(str(d.get("direction","")))}">'
            f'{html.escape(str(d.get("aspect","")))}</li>'
            for d in a.get("drivers", [])
        )
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <span class="badge {html.escape(ev['type'])}">{html.escape(ev['type'])}</span>
            <span class="ev-title">{html.escape(ev['title'])}</span>
            <span class="ev-date">{ev['date']}</span>
          </div>
          <div class="metrics">
            <span>Approval (LLM): <b>{llm_html}</b></span>
            <span>Steam before→after: {delta_html or '—'}</span>
            <span>Divisiveness: <b>{div_html}</b></span>
            <span>n={a.get('n_reviews', 0)}</span>
          </div>
          <p class="summary">{html.escape(a.get('summary') or '')}</p>
          <ul class="drivers">{drivers}</ul>
        </div>""")
    return "\n".join(cards) or '<p class="muted">No event windows were analyzed (try a higher --max-pages).</p>'


def render_html(timeline: dict[str, Any]) -> str:
    cov = timeline.get("coverage", {})
    walked = cov.get("reviews_walked", 0)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(timeline['name'])} — Sentiment Journey</title>
<style>
  :root {{ --bg:#0f1117; --panel:#171a23; --line:#2a2f3a; --text:#e6e8ee; --muted:#8b90a0;
           --curve:#5b9dff; --up:#46c98b; --down:#ef6f6f; --ev:#3a3f4c; --evon:#f4b740; }}
  body {{ background:var(--bg); color:var(--text); font:14px/1.5 system-ui,sans-serif; margin:0; padding:28px; }}
  h1 {{ font-size:20px; margin:0 0 2px; }} .sub {{ color:var(--muted); margin:0 0 18px; }}
  .chart {{ width:100%; height:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
  .grid {{ stroke:var(--line); stroke-width:1; }} .grid.faint {{ stroke:#20242e; }}
  .grid.mid {{ stroke:#3a4150; stroke-dasharray:4 4; }}
  .ylab {{ fill:var(--muted); font-size:11px; text-anchor:end; }}
  .xlab {{ fill:var(--muted); font-size:11px; text-anchor:middle; }}
  .curve {{ fill:none; stroke:var(--curve); stroke-width:2; }}
  .ev {{ stroke:var(--ev); stroke-width:1; }} .ev.analyzed {{ stroke:var(--evon); stroke-width:1.5; }}
  .evdot {{ fill:var(--evon); }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; margin-top:18px; }}
  .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
  .card-head {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
  .ev-title {{ font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .ev-date {{ color:var(--muted); font-size:12px; }}
  .badge {{ font-size:10px; text-transform:uppercase; padding:2px 6px; border-radius:5px; background:#222736; color:var(--muted); }}
  .badge.patch {{ background:#1c3a2c; color:var(--up); }} .badge.launch {{ background:#3a2c1c; color:var(--evon); }}
  .metrics {{ display:flex; flex-wrap:wrap; gap:10px 16px; font-size:12px; color:var(--muted); }}
  .metrics b {{ color:var(--text); }}
  .delta {{ margin-left:6px; padding:1px 6px; border-radius:5px; font-weight:600; }}
  .delta.up {{ background:#13301f; color:var(--up); }} .delta.down {{ background:#311717; color:var(--down); }}
  .summary {{ margin:8px 0 6px; }}
  .drivers {{ list-style:none; padding:0; margin:0; display:flex; flex-wrap:wrap; gap:6px; }}
  .drivers li {{ font-size:12px; padding:2px 8px; border-radius:12px; background:#222736; }}
  .drivers li.positive {{ background:#13301f; color:var(--up); }}
  .drivers li.negative {{ background:#311717; color:var(--down); }}
  .muted {{ color:var(--muted); }}
</style></head>
<body>
  <h1>{html.escape(timeline['name'])} — Sentiment Journey</h1>
  <p class="sub">Approval over time (Steam review histogram). Gold markers = LLM-analyzed event
     windows (±{timeline['events'][0]['analysis']['window_days'] if timeline.get('events_analyzed') else 5}d).
     Crawled {walked} recent reviews · generated {timeline['generated_at'][:10]}.</p>
  {_svg(timeline)}
  <div class="cards">{_event_cards(timeline)}</div>
  <script>window.__TIMELINE__ = {json.dumps(timeline)};</script>
</body></html>"""

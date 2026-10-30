"""The run report: one HTML page with the results first, then each call's evidence and audio."""

from __future__ import annotations

import json
from collections import Counter
from html import escape
from pathlib import Path

from .grade import SUBCATEGORIES
from .scenarios import BY_ID

CSS = """
:root { --bg:#fbfaf7; --fg:#1d1c1a; --muted:#6b675f; --line:#e4e0d8; --card:#ffffff;
  --pass:#2f7d4f; --fail:#b3261e; --ear:#2b5fb3; --brain:#8a4fb3; --hotel:#f1ede4; --prism:#e8eefb; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#171615; --fg:#ece9e3;
  --muted:#a29d93; --line:#34312c; --card:#201f1d; --pass:#6cc58f; --fail:#f2867d; --ear:#8fb3f2;
  --brain:#c79bea; --hotel:#2a2824; --prism:#1f2a3d; } }
:root[data-theme="dark"] { --bg:#171615; --fg:#ece9e3; --muted:#a29d93; --line:#34312c; --card:#201f1d;
  --pass:#6cc58f; --fail:#f2867d; --ear:#8fb3f2; --brain:#c79bea; --hotel:#2a2824; --prism:#1f2a3d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:1040px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:36px 0 10px; } h3 { font-size:16px; margin:0 0 6px; }
.muted { color:var(--muted); } .pass { color:var(--pass); font-weight:600; } .fail { color:var(--fail); font-weight:600; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; margin:18px 0; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }
.tile b { display:block; font-size:22px; } .tile span { color:var(--muted); font-size:13px; }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:13px; color:var(--muted); font-weight:600; }
.wrap { overflow-x:auto; }
.tag { display:inline-block; font-size:12px; padding:1px 7px; border-radius:10px; border:1px solid currentColor; }
.ear { color:var(--ear); } .brain { color:var(--brain); }
details { background:var(--card); border:1px solid var(--line); border-radius:8px; margin:10px 0; padding:10px 14px; }
summary { cursor:pointer; font-weight:600; }
.t { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
.row { display:grid; grid-template-columns:72px 1fr; gap:8px; padding:3px 6px; border-radius:4px; }
.row.hotel { background:var(--hotel); } .row.prism { background:var(--prism); }
.row.heard, .row.tool, .row.tool_result, .row.event, .row.fault { color:var(--muted); font-size:13px; }
audio { width:100%; margin:8px 0; }
"""


def ms(value: float | None) -> str:
    return "–" if value is None else f"{value / 1000:.1f} s"


def tag(category: str, sub: str = "") -> str:
    return f'<span class="tag {category}">{escape(category)}{" / " + escape(sub) if sub else ""}</span>'


def timeline(result: dict) -> str:
    rows = []
    for e in result["timeline"]:
        who = e["who"]
        if who == "hotel":
            label = f"<b>Hotel</b> ({escape(e['mode'])}){' [cut off]' if e.get('cut') else ''}: "
        elif who == "prism":
            cut = f" [cut off at {ms(e['played_ms'])} of {ms(e['total_ms'])}]" if e["cut"] else ""
            filler = ' <span class="tag">filler, no text event</span>' if e.get("no_text_event") else ""
            label = f"<b>Prism</b>{cut}{filler}: "
        elif who == "heard":
            label = "Prism heard: "
        elif who == "tool":
            label = f"Prism called <code>{escape(e['text'])}</code> {escape(json.dumps(e.get('args', {})))}"
            rows.append(f'<div class="row tool"><span class="t">{ms(e["t_ms"])}</span><span>{label}</span></div>')
            continue
        else:
            label = f"{escape(who)}: "
        rows.append(f'<div class="row {who}"><span class="t">{ms(e["t_ms"])}</span><span>{label}{escape(e["text"])}</span></div>')
    return "\n".join(rows)


def write_report(out: Path, results: list[dict], grades: list[dict]) -> None:
    by_name = {r.get("name", r["scenario"]): r for r in results}
    n = len(grades)
    passed = sum(g["passed"] for g in grades)

    def check_ok(g, check_id):
        return next((c["passed"] for c in g["checks"] if c["id"] == check_id), None)

    right_status = sum(bool(check_ok(g, "status")) for g in grades)
    numbers = [check_ok(g, "cancellation_number") for g in grades if check_ok(g, "cancellation_number") is not None]
    critical = sum(f["critical"] for g in grades for f in g["failures"])
    gaps = [g["metrics"]["reply_gap_ms_median"] for g in grades if g["metrics"]["reply_gap_ms_median"] is not None]
    gap = sorted(gaps)[len(gaps) // 2] if gaps else None
    options = results[0]["options"] if results else {}
    line = "8 kHz mu-law phone line" if options.get("phone") else "16 kHz clean line"

    counts = Counter((f["category"], f["subcategory"]) for g in grades for f in g["failures"])
    calls_hit = {
        key: sum(any((f["category"], f["subcategory"]) == key for f in g["failures"]) for g in grades)
        for key in counts
    }
    error_rows = "".join(
        f"<tr><td>{tag(cat)}</td><td>{escape(sub)}</td><td>{counts[(cat, sub)]}</td><td>{calls_hit[(cat, sub)]} of {n}</td></tr>"
        for cat in SUBCATEGORIES
        for sub in SUBCATEGORIES[cat]
        if counts[(cat, sub)]
    ) or '<tr><td colspan="4">No errors.</td></tr>'

    call_rows = []
    sections = []
    for g in grades:
        r = by_name[g["name"]]
        s = BY_ID[g["scenario"]]
        recorded = next((e["args"] for e in reversed(r["timeline"]) if e["who"] == "tool" and e["text"] == "record_outcome"), {})
        got = recorded.get("status", "none")
        if recorded.get("cancellation_number"):
            got += f" · {recorded['cancellation_number']}"
        if recorded.get("fee"):
            got += f" · {recorded['fee']}"
        want = s.expect.status + (f" · {s.expect.cancellation_number}" if s.expect.cancellation_number else "")
        want += f" · {s.expect.fee_dollars} dollars" if s.expect.fee_dollars is not None else ""
        verdict = '<span class="pass">pass</span>' if g["passed"] else f'<span class="fail">{len(g["failures"])} errors</span>'
        call_rows.append(
            f'<tr><td><a href="#{escape(g["name"])}">{escape(g["name"])}</a><br><span class="muted">{escape(s.title)}</span></td>'
            f"<td>{verdict}</td><td>{escape(want)}</td><td>{escape(got)}</td><td>{g['metrics']['duration_s']} s</td></tr>"
        )
        fails = "".join(
            f"<tr><td>{tag(f['category'], f['subcategory'])}{' <b class=fail>critical</b>' if f['critical'] else ''}</td>"
            f"<td>{escape(f['what'])}<br><span class='muted'>{escape(f['detail'])}</span></td></tr>"
            for f in g["failures"]
        ) or '<tr><td colspan="2">None.</td></tr>'
        checks = "".join(
            f"<tr><td>{'<span class=pass>pass</span>' if c['passed'] else '<span class=fail>fail</span>'}</td>"
            f"<td>{escape(c['name'])}</td><td class='muted'>{escape(c['detail'])}</td></tr>"
            for c in g["checks"]
        )
        rubric = {i["id"]: i for i in g.get("rubric", [])}
        verdicts = "".join(
            f"<tr><td>{escape(v['verdict'])}</td><td>{escape(rubric.get(v['id'], {}).get('text', v['id']))}</td>"
            f"<td class='muted'>{escape(v['evidence'])}</td></tr>"
            for v in g.get("judge", {}).get("rubric", [])
        )
        summary = g.get("judge", {}).get("summary", "Not judged.")
        m = g["metrics"]
        sections.append(f"""
<details id="{escape(g['name'])}" {'' if g['passed'] else 'open'}>
<summary>{escape(g['name'])}: {escape(s.title)} · {verdict}</summary>
<p>{escape(summary)}</p>
<audio controls preload="none" src="{escape(g['name'])}.wav"></audio>
<p class="muted">Left channel: the hotel. Right channel: Prism. Duration {m['duration_s']} s · word error rate {m['wer']:.0%} ·
Prism's reply gap (median) {ms(m['reply_gap_ms_median'])} · simulated staff think time (median) {ms(m['staff_think_ms_median'])} ·
call ended because: {escape(r['hangup'] or str(r.get('error')))}</p>
<h3>Errors</h3><div class="wrap"><table>{fails}</table></div>
<h3 style="margin-top:14px">Code checks</h3><div class="wrap"><table>{checks}</table></div>
{f'<h3 style="margin-top:14px">Rubric (written by Claude, graded by Claude)</h3><div class="wrap"><table>{verdicts}</table></div>' if verdicts else ''}
<h3 style="margin-top:14px">Transcript</h3>
<div>{timeline(r)}</div>
</details>""")

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prism hotel calls</title><style>{CSS}</style></head>
<body><main>
<h1>Prism hotel calls: {passed} of {n} passed</h1>
<p class="muted">Run {escape(out.name)} · {line} · Prism voice {escape(options.get('voice', ''))} · simulated hotels (Claude plus Rime TTS), no real calls</p>
<div class="tiles">
<div class="tile"><b>{passed} / {n}</b><span>calls with no errors</span></div>
<div class="tile"><b>{right_status} / {n}</b><span>right outcome recorded</span></div>
<div class="tile"><b>{sum(numbers)} / {len(numbers)}</b><span>cancellation numbers exact</span></div>
<div class="tile"><b>{critical}</b><span>critical errors</span></div>
<div class="tile"><b>{ms(gap)}</b><span>Prism's reply gap, median</span></div>
</div>
<h2>Errors by category</h2>
<p class="muted">Ear: Prism misheard or mistimed. Brain: Prism heard right but chose wrong.</p>
<div class="wrap"><table><tr><th>Category</th><th>Subcategory</th><th>Errors</th><th>Calls with it</th></tr>{error_rows}</table></div>
<h2>Calls</h2>
<div class="wrap"><table><tr><th>Call</th><th>Result</th><th>Expected</th><th>Prism recorded</th><th>Length</th></tr>{''.join(call_rows)}</table></div>
<h2>Each call</h2>
{''.join(sections)}
</main></body></html>"""
    (out / "report.html").write_text(html)

#!/usr/bin/env python3
"""Render the community-poll tally into the README block and docs/poll.json.

Reads live reaction counts from the pinned poll issue (via claudit_scan.poll_counts,
which shells out to `gh`) and rewrites:
  - the <!-- POLL:START -->…<!-- POLL:END --> block in README.md
  - docs/poll.json  (consumed by the GitHub Pages site)

Run by .github/workflows/poll.yml on a schedule; also runnable locally.
"""
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit_scan as cs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
POLL_JSON = os.path.join(ROOT, "docs", "poll.json")
COUNTER_JSON = os.path.join(ROOT, "docs", "counter.json")
HISTORY_JSON = os.path.join(ROOT, "docs", "counter-history.json")
TREND_SVG = os.path.join(ROOT, "docs", "trend.svg")
START, END = "<!-- POLL:START -->", "<!-- POLL:END -->"
CSTART, CEND = "<!-- COUNTER:START -->", "<!-- COUNTER:END -->"
ISSUE_URL = f"https://github.com/{cs.POLL_REPO}/issues/{cs.POLL_ISSUE}"
# Every ClAudit-filed issue body ends with this marker, so one repo-wide search counts
# the open reports across ALL users (not just one).
SEARCH_URL = (f"https://github.com/{cs.DEFAULT_REPO}/issues?q="
              "is%3Aissue+is%3Aopen+%22Filed+automatically+by+ClAudit%22")


def kind_counts():
    """Per-kind counts from the issue LIST (title-based, reliable). Full-text 'harness' search is
    polluted by dup-bot comments that quote harness titles, so we classify by the issue's own title.
    Returns {open_api, closed_api, harness} or None: cyber/aup are the real API false positives;
    harness is the withdrawn class, tracked separately and NOT counted as a closed API ticket."""
    out = subprocess.run(
        ["gh", "issue", "list", "-R", cs.DEFAULT_REPO, "--search", '"Filed automatically by ClAudit"',
         "--state", "all", "--limit", "800", "--json", "state,title"],
        capture_output=True, text=True)
    try:
        items = json.loads(out.stdout or "[]")
    except (ValueError, AttributeError):
        return None
    d = {"open_api": 0, "closed_api": 0, "harness": 0}
    for it in items:
        t = (it.get("title", "") or "").lower()
        is_open = (it.get("state", "") or "").lower() == "open"
        if "[harness]" in t:
            d["harness"] += 1
        elif "[cyber]" in t or "[aup]" in t:
            d["open_api" if is_open else "closed_api"] += 1
    return d


def open_report_count():
    """Live count of OPEN cyber/aup (real API false-positive) issues."""
    d = kind_counts()
    return d["open_api"] if d else None


def append_history(point, when):
    """Append a per-kind data point {open_api, closed_api, harness} to the time series
    (one point per UTC hour, capped ~60 days)."""
    hist = []
    if os.path.exists(HISTORY_JSON):
        try:
            hist = json.load(open(HISTORY_JSON))
        except Exception:
            hist = []
    point = {"t": when, **point}
    if hist and hist[-1].get("t", "")[:13] == when[:13]:
        hist[-1] = point                      # same hour -> overwrite, don't pile up
    else:
        hist.append(point)
    hist = hist[-1440:]
    with open(HISTORY_JSON, "w") as fh:
        json.dump(hist, fh)
    return hist


OPEN_FP = "#4aa3ff"       # cyan: open cyber/aup (real API false positives)
CLOSED_FP = "#3fb950"     # green: closed cyber/aup (Anthropic acting)
HARNESS_C = "#8a5a5a"     # muted red: harness, withdrawn; tracked separately, NOT a closed ticket


def _g(p, key):
    return p.get(key, p.get({"open_api": "open", "closed_api": "closed"}.get(key, key), 0)) or 0


def render_trend_svg(hist):
    """Dependency-free SVG line chart, normalized to API blocks. Three lines: open cyber/aup false
    positives (cyan), closed cyber/aup (green, Anthropic acting), and the withdrawn harness class
    (muted, a separate line that does not count toward closed)."""
    W, H, pad = 700, 234, 36
    if not hist:
        hist = [{"t": "", "open_api": 0, "closed_api": 0, "harness": 0}]
    n = len(hist)
    ymax = max(1, max(max(_g(p, "open_api"), _g(p, "closed_api"), _g(p, "harness")) for p in hist))

    def coord(i, v):
        fx = 0.5 if n == 1 else i / (n - 1)
        return pad + (W - 2 * pad) * fx, H - pad - (H - 2 * pad) * (v / ymax)

    def seg(key, color):
        pts = [coord(i, _g(hist[i], key)) for i in range(n)]
        line = (f'<polyline fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" '
                f'points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}"/>' if n > 1 else "")
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>' for x, y in pts)
        return line + dots

    grid = "".join(
        f'<line x1="{pad}" y1="{H - pad - (H - 2 * pad) * f:.1f}" x2="{W - pad}" '
        f'y2="{H - pad - (H - 2 * pad) * f:.1f}" stroke="#2a2e37" stroke-width="1"/>'
        f'<text x="{pad - 6}" y="{H - pad - (H - 2 * pad) * f + 4:.1f}" fill="#6b7280" '
        f'font-size="11" text-anchor="end">{round(ymax * f)}</text>'
        for f in (0, 0.5, 1.0))
    first, last = hist[0]["t"][:10], hist[-1]["t"][:10]
    cur = hist[-1]
    building = (f'<text x="{W / 2:.0f}" y="{H / 2 + 20:.0f}" text-anchor="middle" fill="#6b7280" '
                f'font-family="system-ui,sans-serif" font-size="12">trend builds hourly: '
                f'{n} data point{"s" if n != 1 else ""} so far</text>' if n < 3 else "")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img">
<rect width="{W}" height="{H}" rx="12" fill="#16181d"/>
<text x="{pad}" y="22" fill="#e8eaed" font-family="system-ui,sans-serif" font-size="14" font-weight="700">Is Anthropic acting? cyber/aup false positives over time</text>
{grid}
{building}
{seg("harness", HARNESS_C)}
{seg("closed_api", CLOSED_FP)}
{seg("open_api", OPEN_FP)}
<text x="{pad}" y="{H - 9}" fill="#6b7280" font-size="11">{first}</text>
<text x="{W - pad}" y="{H - 9}" fill="#6b7280" font-size="11" text-anchor="end">{last}</text>
<text x="{pad}" y="{H - 22}" font-family="system-ui,sans-serif" font-size="11">
<tspan fill="{OPEN_FP}">&#9679; open FPs</tspan>  <tspan fill="{CLOSED_FP}">&#9679; closed (Anthropic acting)</tspan>  <tspan fill="{HARNESS_C}">&#9679; harness withdrawn</tspan></text>
<text x="{W - pad}" y="22" text-anchor="end" font-family="system-ui,sans-serif" font-size="12">
<tspan fill="{OPEN_FP}">open {_g(cur, "open_api")}</tspan>  <tspan fill="{CLOSED_FP}">closed {_g(cur, "closed_api")}</tspan>  <tspan fill="{HARNESS_C}">harness {_g(cur, "harness")}</tspan></text>
</svg>'''


def _bar(pct, cells=10):
    fill = round(pct / 100 * cells)
    return "█" * fill + "░" * (cells - fill)


def render_md(counts, when):
    total = counts["total"] or 1
    rows = []
    for key, _content, emoji, meaning in cs.POLL_OPTS:
        n = counts[key]
        pct = round(100 * n / total)
        rows.append(f"| {emoji} {meaning} | `{_bar(pct)}` | **{pct}%** ({n}) |")
    head = (f"**Will Anthropic fix Claude Code's false-positive blocking, or will it stay "
            f"broken?**  ·  _{counts['total']} vote(s), updated {when} UTC_")
    table = "| | | |\n|---|---:|---|\n" + "\n".join(rows)
    return (f"{START}\n{head}\n\n{table}\n\n"
            f"🗳️ **[Cast your vote →]({ISSUE_URL})**. React 👍 / 👎 / 👀 on the pinned issue "
            f"(or vote in one click from the ClAudit app).\n{END}")


def main():
    counts = cs.poll_counts()
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(POLL_JSON), exist_ok=True)
    with open(POLL_JSON, "w") as fh:
        json.dump({**counts, "updated": when + " UTC",
                   "issue": ISSUE_URL,
                   "options": [{"key": k, "emoji": e, "meaning": m} for k, _c, e, m in cs.POLL_OPTS]},
                  fh, indent=2)
    # live per-kind counter (cyber/aup only) + historical trend graphic
    d = kind_counts()
    n = d["open_api"] if d else None
    if n is not None:
        with open(COUNTER_JSON, "w") as fh:   # shields.io endpoint badge
            json.dump({"schemaVersion": 1, "label": "open false-positive reports",
                       "message": f"{n}", "color": "red"}, fh, indent=2)
        hist = append_history(d, when + " UTC")
        with open(TREND_SVG, "w") as fh:
            fh.write(render_trend_svg(hist))
        counter_block = (
            f"{CSTART}\n### 📊 {n} open false-positive blocks reported by ClAudit right now\n\n"
            f"Real cyber/aup API false positives across **all** ClAudit users, live from "
            f"[`anthropics/claude-code`]({SEARCH_URL}). **{d['closed_api']} closed by Anthropic** · "
            f"_updated {when} UTC_\n\n"
            f"[![ClAudit reports over time](docs/trend.svg)]({SEARCH_URL})\n\n"
            f"<sub>Three lines: open cyber/aup false positives (cyan), closed by Anthropic (green), and "
            f"the {d['harness']} auto-mode-classifier (harness) reports ClAudit withdrew (muted), "
            f"tracked separately and not counted as closed tickets.</sub>\n{CEND}")

    block = render_md(counts, when)
    with open(README) as fh:
        text = fh.read()
    if START in text and END in text:
        pre, rest = text.split(START, 1)
        _old, post = rest.split(END, 1)
        text = pre + block + post
    else:
        sys.stderr.write("WARN: POLL markers not found in README; skipping README update\n")
    if n is not None and CSTART in text and CEND in text:
        pre, rest = text.split(CSTART, 1)
        _old, post = rest.split(CEND, 1)
        text = pre + counter_block + post
    elif n is not None:
        sys.stderr.write("WARN: COUNTER markers not found in README; skipping counter update\n")
    with open(README, "w") as fh:
        fh.write(text)
    print(f"poll: 👍{counts['plus']} 👎{counts['minus']} 👀{counts['eyes']} (total {counts['total']}) "
          f"| open reports: {n}")


if __name__ == "__main__":
    main()

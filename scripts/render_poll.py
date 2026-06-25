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


def _count(scope=""):
    q = f'repo:{cs.DEFAULT_REPO} "Filed automatically by ClAudit" is:issue {scope}'.strip()
    out = subprocess.run(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}",
                          "--jq", ".total_count"], capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def open_report_count():
    """Live count of OPEN issues filed by ClAudit across all users."""
    return _count("is:open")


def append_history(open_n, total_n, when):
    """Append today's data point to the time series (one point per UTC hour, capped ~60 days)."""
    hist = []
    if os.path.exists(HISTORY_JSON):
        try:
            hist = json.load(open(HISTORY_JSON))
        except Exception:
            hist = []
    point = {"t": when, "open": open_n, "total": total_n, "closed": max(0, total_n - open_n)}
    if hist and hist[-1].get("t", "")[:13] == when[:13]:
        hist[-1] = point                      # same hour -> overwrite, don't pile up
    else:
        hist.append(point)
    hist = hist[-1440:]
    with open(HISTORY_JSON, "w") as fh:
        json.dump(hist, fh)
    return hist


def render_trend_svg(hist):
    """Dependency-free SVG line chart: total filed (grey), still-open (red), closed (green)."""
    W, H, pad = 680, 220, 34
    if not hist:
        hist = [{"t": "", "open": 0, "total": 0, "closed": 0}]
    n = len(hist)
    ymax = max(1, max(p["total"] for p in hist))

    def xy(i, v):
        x = pad + (W - 2 * pad) * (i / max(1, n - 1))
        y = H - pad - (H - 2 * pad) * (v / ymax)
        return f"{x:.1f},{y:.1f}"

    def poly(key, color):
        pts = " ".join(xy(i, p[key]) for i, p in enumerate(hist))
        return (f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
                f'stroke-linejoin="round" points="{pts}"/>')

    grid = "".join(
        f'<line x1="{pad}" y1="{H - pad - (H - 2 * pad) * f:.1f}" x2="{W - pad}" '
        f'y2="{H - pad - (H - 2 * pad) * f:.1f}" stroke="#2a2e37" stroke-width="1"/>'
        f'<text x="{pad - 6}" y="{H - pad - (H - 2 * pad) * f + 4:.1f}" fill="#6b7280" '
        f'font-size="11" text-anchor="end">{round(ymax * f)}</text>'
        for f in (0, 0.5, 1.0))
    first, last = hist[0]["t"][:10], hist[-1]["t"][:10]
    cur = hist[-1]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img">
<rect width="{W}" height="{H}" rx="12" fill="#16181d"/>
<text x="{pad}" y="22" fill="#e8eaed" font-family="system-ui,sans-serif" font-size="14" font-weight="700">Is Anthropic acting? — ClAudit reports over time</text>
{grid}
{poly("total", "#6b7280")}
{poly("closed", "#3fb950")}
{poly("open", "#f85149")}
<text x="{pad}" y="{H - 8}" fill="#6b7280" font-size="11">{first}</text>
<text x="{W - pad}" y="{H - 8}" fill="#6b7280" font-size="11" text-anchor="end">{last}</text>
<text x="{W - pad}" y="22" text-anchor="end" font-family="system-ui,sans-serif" font-size="12">
<tspan fill="#6b7280">filed {cur["total"]}</tspan>  <tspan fill="#f85149">open {cur["open"]}</tspan>  <tspan fill="#3fb950">closed {cur["closed"]}</tspan></text>
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
    head = (f"**Will Anthropic fix Claude Code's false-positive blocking — or will it stay "
            f"broken?**  ·  _{counts['total']} vote(s), updated {when} UTC_")
    table = "| | | |\n|---|---:|---|\n" + "\n".join(rows)
    return (f"{START}\n{head}\n\n{table}\n\n"
            f"🗳️ **[Cast your vote →]({ISSUE_URL})** — react 👍 / 👎 / 👀 on the pinned issue "
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
    # live cross-user counter of open ClAudit-filed reports + historical trend graphic
    n = open_report_count()
    if n is not None:
        with open(COUNTER_JSON, "w") as fh:   # shields.io endpoint badge
            json.dump({"schemaVersion": 1, "label": "open false-positive reports",
                       "message": f"{n}", "color": "red"}, fh, indent=2)
        total = _count() or n                 # all states (filed-ever)
        hist = append_history(n, total, when + " UTC")
        with open(TREND_SVG, "w") as fh:
            fh.write(render_trend_svg(hist))
        closed = max(0, total - n)
        counter_block = (
            f"{CSTART}\n### 📊 {n} open false-positive blocks reported by ClAudit right now\n\n"
            f"Across **all** ClAudit users, live from [`anthropics/claude-code`]({SEARCH_URL}) — "
            f"**{total} filed**, **{closed} closed** · _updated {when} UTC_\n\n"
            f"[![ClAudit reports over time](docs/trend.svg)]({SEARCH_URL})\n\n"
            f"<sub>Open (red) falling toward closed (green) = Anthropic is acting.</sub>\n{CEND}")

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

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
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit_scan as cs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
POLL_JSON = os.path.join(ROOT, "docs", "poll.json")
START, END = "<!-- POLL:START -->", "<!-- POLL:END -->"
ISSUE_URL = f"https://github.com/{cs.POLL_REPO}/issues/{cs.POLL_ISSUE}"


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
    block = render_md(counts, when)
    with open(README) as fh:
        text = fh.read()
    if START in text and END in text:
        pre, rest = text.split(START, 1)
        _old, post = rest.split(END, 1)
        text = pre + block + post
    else:
        sys.stderr.write("WARN: POLL markers not found in README; skipping README update\n")
    with open(README, "w") as fh:
        fh.write(text)
    print(f"poll: 👍{counts['plus']} 👎{counts['minus']} 👀{counts['eyes']} (total {counts['total']})")


if __name__ == "__main__":
    main()

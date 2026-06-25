#!/usr/bin/env python3
"""Build a consolidated pattern report from everything ClAudit has filed — a LOCAL artifact.

NOTE: a single aggregate/tracking issue is NOT bespoke (it has no incident Request ID of its own),
so it is deliberately NOT part of the default flow — every block is filed as its own distinct issue
with its own Request ID. This script stays as an opt-in local roll-up (docs/pattern-report.md);
--file / --update remain available but are not used by the watcher.

  python3 scripts/pattern_report.py                       # write docs/pattern-report.md (local)
  python3 scripts/pattern_report.py --file -R <repo>      # (opt-in) file it as one tracking issue
  python3 scripts/pattern_report.py --update <issue#>     # (opt-in) refresh an existing one
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit_scan as cs   # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "pattern-report.md")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="store_true", help="file the report as one tracking issue")
    ap.add_argument("--update", type=int, metavar="ISSUE", help="refresh an existing tracking issue")
    ap.add_argument("-R", "--repo", default=cs.DEFAULT_REPO)
    args = ap.parse_args()

    rows = cs.load_issue_rows()
    md = cs.pattern_report_md(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write(md)
    nreq = len({q for r in rows for q in (r.get("reqs") or [])})
    print(f"wrote {OUT} ({len(rows)} reports, {nreq} Request IDs)")

    if args.update:
        print("refreshed:", cs.update_tracking(args.repo, args.update), "reports ->", f"#{args.update}")
    elif args.file:
        title = (f"[Tracking] Classifier false-positives on authorized admin of the reporter's own "
                 f"infrastructure — {nreq} Request IDs")
        print("filed:", cs.gh_create(args.repo, title, md))


if __name__ == "__main__":
    main()

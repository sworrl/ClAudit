# Changelog

All notable changes to ClAudit are documented here. Each filed issue records the ClAudit
version that submitted it (in the issue footer and in `~/.claude/claudit/issues.jsonl`).

## [1.2.0] — 2026-06-25
- Community board: every false-positive issue on the repo (all authors, open + closed),
  newest-first, with exact local timestamps.
- Filters: Mine/All, Open/Closed, title search. Ownership colors (yours vs other ClAudit users).
- Click any row to open the issue in the browser.
- Removed read-only `--view`; the GUI is always the watcher + board.
- Optional Claude-assisted PII scrub with a saved on/off choice + startup prompt + tray toggle.

## [1.1.0] — 2026-06-25
- Detect Claude Code auto-mode-classifier denials (new `harness` kind).
- "Why this is a false positive" lead, heuristic work-domain tag, conversation leadup in reports.
- User PII denylist (`~/.claude/claudit/scrub.txt`) + more regex patterns; slow `--backfill` drip.

## [1.0.0] — 2026-06-25
Initial release.

- Watch all Claude Code sessions for cybersecurity safety-filter and AUP/Usage-Policy blocks.
- Dedup by triggering prompt; one issue per distinct blocked request, with all Request IDs.
- PII scrubbing; conversation-leadup capture; non-PII work-context.
- Logs (never sends) transient noise: overloaded, rate-limit, usage-limit, other.
- Modes: notify-only (default), `--auto` insta-post, `--backfill` slow drip of the backlog.
- Single-instance lock + reserve-before-post dedup (no races/double-posts).
- PyQt6 tray app + issue dashboard with live GitHub status.
- Every issue links back to the ClAudit repo and records the filing version.
- Local issues database at `~/.claude/claudit/issues.jsonl`.

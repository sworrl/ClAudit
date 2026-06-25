# Changelog

All notable changes to ClAudit are documented here. Each filed issue records the ClAudit
version that submitted it (in the issue footer and in `~/.claude/claudit/issues.jsonl`).

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

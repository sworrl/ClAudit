# Changelog

All notable changes to ClAudit are documented here. Each filed issue records the ClAudit
version that submitted it (in the issue footer and in `~/.claude/claudit/issues.jsonl`).

## [1.6.0] — 2026-06-25
- **Burn-tokens mode** (`--burn-tokens`): Claude writes a bespoke, specific title + explanation per report — the strongest PII defense (composed, not copied). On by default for the local config.
- **PII hardening:** removed conversation-leadup, project-path, and prompt-hint sections from public posts (kept locally); scrub dash-encoded usernames; username/org added to the local denylist.
- **Dedup guard** (`--dedup-guard [--apply]`): LLM judges dup-bot flags on facts; comments only on genuinely-distinct issues (dry-run by default).
- GUI shows the running git commit; verbose README rewrite; pre-commit hook auto-bumps the version.

## [1.5.1] — 2026-06-25
- **PII fix:** scrub usernames in dash-encoded session/tmp paths (`-var-home-USER-…`, the `claude-1000` task dirs) that the home-path regex missed. (Mitigated two already-posted issues.)

## [1.5.0] — 2026-06-25
- **Relicensed to GNU GPL v3.0** (was MIT).
- Self-restart on update: a running GUI watches its own source and relaunches when it changes
  (e.g. after a `git pull`), so it's never running stale code.

## [1.4.0] — 2026-06-25
- **Fix:** LLM PII scrub no longer over-redacts — Request IDs (`req_…`) and the words Claude/
  Anthropic/ClAudit/GitHub are hard-protected and always survive. Titles keep their Request ID.
- Prominent backfill **progress bar** in the window (filed / total / next-drip / pace).
- Performance: short scan cache so backfill doesn't re-read every session file per drip.

## [1.3.0] — 2026-06-25
- **Fix:** resolve `gh`/`claude` on PATH when launched from a desktop icon (board was empty / posts silently failed under a minimal PATH).
- Live blocks always post the moment they're seen, independent of the backfill schedule.
- Adaptive backfill: drips as fast as GitHub allows, exponential back-off on rate-limit, speeds back up when safe. `--backfill-interval` is now starting **seconds** (was minutes).
- Live backfill readout in the window: filed / left / next-drip / current pace.

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

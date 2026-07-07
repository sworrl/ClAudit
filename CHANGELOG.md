# Changelog

All notable changes to ClAudit are documented here. Each filed issue records the ClAudit
version that submitted it (in the issue footer and in `~/.claude/claudit/issues.jsonl`).

## [2.0.107] — 2026-07-07
**Daily per-issue recheck + bot-only targeting + Haiku default.**
- **Every open issue rechecked daily** — `daily_recheck` verifies each open ClAudit issue once a day at its own pseudo-random minute (hash of the issue number; static, so consecutive checks are exactly ≤24h apart). The cloud sweep windows by the previous successful run, so throttled/missed cron fires are caught up, never skipped.
- **Only bot auto-close attempts are defended** — dup-flag comments must be authored by the actions bot (or another `[bot]`); a human maintainer writing "possible duplicate" is a legitimate response and never triggers a defense. The label-only defense path is gone for the same reason (a human-applied label isn't an auto-close attempt). Applies to the sweep, the daily recheck, the verifier, and the GUI's manual 👎.
- **Haiku 4.5 at medium effort is the default LLM** for compose/scrub/gate calls — verified fast and solid in real use, at a fraction of Sonnet's cost. (Config `llm_model`/`llm_effort` still override.)

## [2.0.106] — 2026-07-07
**Bulletproof defense mode.** No single point of failure between a dup-bot flag and its answer:
- **Third, wording-independent listing** — flagged issues are found via the `duplicate` label ∪ the bot's comment text ∪ `commenter:app/github-actions`, so a bot rewording (or another label change) can't hide flags again.
- **Verification gate in the cloud sweep** — after each pass the workflow re-scans read-only (`undefended_flags`) and **fails the run** if any flag in the auto-close window is still unanswered, so GitHub emails on breakage instead of issues silently closing. An expired `CLAUDIT_PAT` also fails the run instead of no-opping.
- **Never guess on API failure** — a failed `gh issue view` now skips the issue for retry next pass (it previously fell through as "label-only" and could double-post); the verifier counts unverifiable issues as at-risk, not safe.
- **Real limits** — sweep caps raised to 1000 (the label-only cap of 100/200 was below the live issue counts); `gh` call timeout 30s → 90s for large searches.
- Label-only defense now requires the `duplicate` label to actually be present (the commenter listing surfaces issues with unrelated bot comments; those are skipped, not "defended").

## [2.0.105] — 2026-07-07
- **Fix: dup-defense missed unlabeled flags.** The dup-bot now posts its "possible duplicate" comment without applying the `duplicate` label, so the label-only listing in `defend_all` / `reopen_dupe_closes` silently skipped those issues and they auto-closed undefended. Both sweeps now union the label listing with a comment-text search (`possible duplicate issues in:comments`), so every flagged issue is answered regardless of labeling.

## [2.1.0] (2.0.1 – 2.0.32) — 2026-06-25
Big feature batch (patch versions auto-bumped per commit; summarized here).

**Community & dashboard**
- **Community poll** — a GitHub-reaction vote ("Will Anthropic fix it?") on a pinned issue, with a live tally in the README, a GitHub Pages site, and **one-click voting inside the app**.
- **Live cross-user report counter** — a shields badge + README block counting *open* ClAudit-filed issues across **all** users (keys on the "Filed automatically by ClAudit" marker), refreshed hourly by a GitHub Action.
- **Historical trend graphic** — a dependency-free SVG line chart (total filed / open / closed over time) so you can see whether Anthropic is acting.

**Honesty & reports**
- **Honesty gate is now opt-in** (`--gate`, off by default) — ClAudit files every genuine block; whether a block was "correct" vs a false positive is the contested thing it exists to surface, so it isn't pre-judged.
- **Never post LLM refusals** — if the burn-tokens composer refuses or editorializes ("not a false positive", "I won't…"), the report falls back to **facts-only** (block type + traceable Request IDs, nothing asserted).
- **Bespoke-only** — every issue is one distinct incident with its own Request ID; no aggregate/"tracking" issues.

**Dedup defense, closures & reopen**
- **Auto-defend** every dup-bot flag (👎 + a factual "not a duplicate" note) — continuous, idempotent, and **fast** (one search per sweep, not a per-issue scan), with retry-on-failure and label-only coverage. GUI toggle (on by default), tray + CLI (`--defend-all`, `--watch --defend`).
- **Closure monitoring** — the GUI shows **why** each issue closed (duplicate / not-planned / completed), and **auto-reopen** (opt-in) reopens issues the dup-bot closed as duplicates — never touching your own closes.

**PII & safety**
- **GUI PII-denylist manager** — add/remove `scrub.txt` terms from the tray or Project tab; the running watcher picks up changes immediately.

**Platform**
- **Self-update from GitHub** — the GUI fetches origin every few minutes and **fast-forward pulls** when clean+behind, then relaunches (never force-updates dirty/diverged checkouts).
- New block-classification signatures (community **PR #5**, co-authored) + a scope guard keeping ordinary refusals non-reportable. Ships a **Claude Code skill** (`skills/claudit/`).

## [2.0.0] — 2026-06-25
Major release.
- GUI: **Project stats** tab (stars + who starred, forks, watchers, your followers).
- **Manual per-issue dedup** — select an issue, click 👎 to mark it not-a-duplicate (live); a 👎✓ marker shows which you've handled. (No blanket auto-fighting of the dup-bot.)
- The honesty gate, burn-tokens bespoke reports, adaptive backfill, the community board with filters/colors/exact-times, and full PII hardening from the 1.x line.
- Self-restart now triggers on a real git commit/pull (not every local edit) — no more restart thrashing.

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

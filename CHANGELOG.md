# Changelog

All notable changes to ClAudit are documented here. Each filed issue records the ClAudit
version that submitted it (in the issue footer and in `~/.claude/claudit/issues.jsonl`).

## [2.6.0] — 2026-09-23
**Plain-text pass on everything published, CI on all three desktops, packaging starters, and the open issues answered.**
- README rewritten. Same facts, shorter, no badges, no emoji, no hook copy. The version now sits on a plain "Current version:" line, which CI, the pre-commit hook, and the tests check. The hourly poll and counter blocks the Action writes into the README were retoned to match (`scripts/render_poll.py`), as were the Pages site, CONTRIBUTING, the Claude Code skill, and the issue templates.
- Issue templates: the bug report asks for `--doctor` output up front; a new "Block signature" template collects the exact block text and Request ID for issue #3-style reports; the poll is a contact link.
- CI runs the test suite on macOS and Windows as well as Linux, and a new `gui-smoke` job installs PyQt6 on each of the three and builds the dialogs and the header meter offscreen (`tests/test_gui_smoke.py`, skipped where PyQt6 is missing). That covers the import-and-construct half of issue #1; the tray icon and notifications still need a person at a real desktop.
- `packaging/`: a Homebrew head formula (`brew install --HEAD --formula ./packaging/homebrew/claudit.rb`) and an Arch `claudit-git` PKGBUILD, both for the CLI tools with the tray app via pipx. Neither has run on a clean machine yet; issue #2 says so.
- Tray menu gains "Run doctor", the same check as `--doctor` in a dialog with a Copy button. `claudit_gui.py --version`.
- GitHub Actions bumped to `checkout@v7`, `setup-python@v7`, `upload-artifact@v7` (Dependabot #8, #9, #10). Dependabot no longer watches pip: the Python dependencies are floors, and raising them to the newest release (#11, #12, #13) would only shut out older systems.

## [2.5.0] — 2026-09-23
**Guard rails around the new usage meter, a one-command environment check, and the loose ends.**
- **Usage guard.** ClAudit's `claude` calls share the plan the meter now reads, so the Settings tab gains a "Pause claude calls above" slider (config `usage_guard_pct`, default 90). Once the 5-hour or 7-day window reaches it, `claude` calls abstain (tandem and the gate already treat an abstaining engine as broken and fall back to templates), `agy` calls continue, and a stderr note is logged at most hourly. The tray warns at 80% and 95% of any window, once per crossing.
- **`claudit_scan.py --doctor`** checks Python, gh sign-in, the `claude` and `agy` CLIs, which engine the config resolves to, the Claude Code login the meter needs, PyQt6, desktop notifications, the transcripts directory, state-dir writability, `config.json`, scrub and mute term counts, the watcher lock (live, stale, or absent), the git checkout's ahead/behind/dirty state, and whether a newer release exists. Exit 1 on a hard failure, so it works as a pre-flight in scripts.
- **Release check for pip installs.** The GUI's self-update needs a git clone. Without `.git` it now asks GitHub for the latest release every few minutes and shows one tray message per new version with the upgrade command.
- **Mute list in the GUI.** `mute.txt` (2.2.3) had no editor; the tray menu, Settings tab, and Project tab now carry "Edit mute list…" next to the PII denylist, with the same add/remove dialog. Edits are picked up live.
- **Windows icon.** `scripts/install-windows.ps1` pointed at `claudit_icon.ico`, which did not exist. `scripts/gen-icon.py` now writes it (multi-size, derived from the PNG; `--ico-only` skips the full regeneration) and the file ships in the repo.
- **Fix: locked issues were retried forever.** Maintainers locked three of the canonical issues that merged reports fold into; every 15-minute pass (desktop and, since 2.3.0, cloud) re-ran the fold comment on each and logged a failure. A rejected comment now checks the REST `locked` flag and records the canonical (or swept report) under `state['__locked__']` instead of retrying. The request IDs stay in the local record.
- Dependabot for GitHub Actions and pip, monthly. Tests for the guard, the release lookup, the doctor rows (offline), and the hourly poll renderer (`render_md`, `append_history`, `render_trend_svg`), which had none. 97 tests, plus one for the locked-canonical fix below.

## [2.4.0] — 2026-09-23
**Usage meter overhaul: real plan windows instead of a dead estimate.** The header 🔥 meter was fed only by the `claude` CLI's per-call USD figure and turned that into a guessed share of a guessed weekly budget. With `llm_engine: "agy"` (the recommended setting since 2.2.5) no call ever carried a dollar figure, the rolling history stayed empty, and the pill read `$0.00/wk · Pro 0%` while the account was at 39% of its weekly window and 62% of its Fable cap.
- **Live plan usage.** `claudit.plan_usage()` reads the 5-hour window, the 7-day window, and any model-scoped weekly limit from Anthropic's usage endpoint, the one Claude Code's `/usage` shows, using the Claude Code login already on the machine (`~/.claude/.credentials.json`, the macOS keychain, or `$CLAUDE_CODE_OAUTH_TOKEN`). Fetched off the UI thread every five minutes, cached in `~/.claude/claudit/usage.json`; a failed refresh keeps the last snapshot and the pill says `· stale` after three misses. No login: the old estimate remains as a labelled fallback (`est.`).
- **The pill now reads `🔥 5h 17% · 7d 39% · Fable 62%`** and fills with the fullest window (amber past 50%, red past 80%). The tooltip carries reset countdowns, ClAudit's own trailing-week calls per engine, and lifetime totals.
- **Per-engine tallies.** `tokens.json` keeps `engines.claude` and `engines.agy` alongside the legacy totals, and every call (agy included, cost or not) lands in the weekly history with its engine and token count, so `weekly_usage()` can report agy's token volume. Old two-field history entries still read.
- **CLI:** `claudit_scan.py --usage` prints the same report headless.
- Tests: endpoint parsing and caching with a mocked login, the no-login and offline paths, per-engine accounting with a legacy file, reset-countdown formatting.

## [2.3.0] — 2026-09-23
**Shipped: the 2.2.x line reaches GitHub, plus releases, a real landing page, and autostart on every platform.** The six commits from 2.2.0 to 2.2.5 (tandem engines, closure intelligence, mute list, agy-only mode) sat unpushed for a month while the hourly counter bot kept committing; this release rebases them onto `main` and adds the delivery plumbing that was missing:
- **Tag-driven releases.** Pushing a `vX.Y.Z` tag builds the sdist and wheel, takes that version's section of this file as the notes, and publishes a GitHub Release with the files attached (`.github/workflows/release.yml`). The tag must match `__version__` or the job fails. `v2.3.0` is the first one.
- **CI actually covers what the README promises.** The test job now runs on Python 3.9, 3.12, and 3.13 (the floor is 3.9; it was only ever tested on 3.12), and a package job builds the wheel, installs it, and runs `claudit-watch --version` / `--help` so a broken entry point cannot ship. It also fails when the README version badge or this changelog disagrees with `__version__`, which is how 2.0.111 stayed on the badge through five releases.
- **Cloud defend runs the closure defender.** The 20-minute GitHub Action now also answers inactivity-bot sweeps (one still-relevant note pointing at the umbrella issue) and folds maintainer-merged duplicates onto their canonical, the same two actions the desktop app has taken since 2.1.0. Both re-parse their own comment markers, so the stateless runner never posts twice. The umbrella body is still refreshed only by the desktop app, which holds the full swept history.
- **Landing page.** `sworrl.github.io/ClAudit` showed only the poll. It now carries the live counter tiles (open, closed by Anthropic, harness withdrawn), the trend chart, the poll, the install snippet, and links to the umbrella issue and releases. The icon path was broken (`../` above the site root); it now loads from the repo.
- **Autostart on macOS and Windows** (closes #4). `scripts/install-macos.sh` installs a launchd user agent that starts the tray app hidden at login, with the shell's PATH so `gh`/`claude`/`agy` resolve; `--uninstall` removes it. `scripts/install-windows.ps1` creates a Start Menu shortcut and, with `-Autostart`, a Startup-folder entry, using `pythonw` so no console window lingers.
- **Pre-commit hook.** Docs-only commits no longer bump the version; the README badge is resynced on every commit, including manual minor/major bumps (it previously only followed auto-bumps).
- Housekeeping: three `open()` calls without a context manager (trend/history reads in the GUI and the poll renderer) now close their handles; `claudit_gui.py` is executable like the other entry points; `build/` and `dist/` are ignored.

## [2.2.5] — 2026-08-22
**agy-only mode keeps the cross-check, spends zero claude quota.** Tandem's claude/Haiku review calls were eating the user's weekly Claude plan (rolling 7-day: $11+ API-equivalent). With `llm_engine: "agy"`, the second voice is now agy itself on a different model (`agy_review_model`, default `gemini-3.1-pro-low`) instead of the claude CLI: PII passes still union two models, composed drafts still get a second-model slop/PII review, and the false-positive gate still takes two votes — all billed to Antigravity. Set `agy_review_model: ""` for a true single-voice run. `--engine tandem` still uses both CLIs for anyone who wants it.

## [2.2.4] — 2026-08-22
**agy calls no longer pollute the chat history.** Every headless `agy -p` run (compose, scrub, gate, verdict) was landing as its own conversation in Antigravity's default history, burying the user's manual conversations and making them hard to resume. All ClAudit agy calls now run under a dedicated Antigravity project (`--project ClAudit`), keeping the default history clean. Config `agy_project` renames it; set it to `""` to restore the old behavior.

## [2.2.3] — 2026-08-21
**Mute list: findings too sensitive to post at all.** The scrub denylist redacts and still posts; some work (e.g. matters in active litigation) must not be described publicly even generically. `~/.claude/claudit/mute.txt` (one substring per line, case-insensitive, `#` comments) now mutes any finding whose block text, blocked prompt, conversation leadup, or project path contains a listed term:
- Muted findings are never filed, never composed, and never sent to ANY LLM CLI — the check runs before the gate, so muted content stays entirely on the machine.
- Enforced at both chokepoints: `should_file` (every auto path — dwell, backfill, watch) and `file_one` (manual pushes refuse too, with a stderr note). A muted Request ID already sitting in the dwell hold can no longer ripen.
- The file is mtime-watched, so a running watcher/GUI picks up edits without a restart.

## [2.2.0] — 2026-08-21
**Tandem engines: agy writes, haiku checks.** ClAudit's LLM calls can now run both installed CLIs together instead of picking one (`--engine tandem`; `auto` resolves to tandem when both `agy` and `claude` are on PATH). agy leads every generation call (it carries the larger token budget); claude (Haiku 4.5, medium effort) only gets short review calls:
- **PII scrubbing on new issues** — `llm_redact` runs the identify-terms pass through EVERY installed engine and unions the findings, so a name one model misses is still caught by the other. Redaction stays deterministic (the models only list terms; the request-ID/vendor-name guard still applies).
- **Composed text is cross-checked** — after agy drafts a title/body/defense comment, claude reviews the draft: PII it finds is redacted in place, and a draft it judges AI-slop is rejected so the caller falls back to its deterministic template. Reviewer breakage never blocks a draft. Every compose prompt now carries the condensed no-ai-slop rules (no em-dashes, no intensifiers, no filler, end on checkable facts, plain register).
- **False-positive gate is a two-model vote** — either engine's clear "correct block" verdict vetoes the filing; a broken engine abstains instead of vetoing.
- **Swept-closure notes can be composed** — `defend_swept` gains `compose=` (on by default wherever burn-tokens is on): the still-relevant note is written per issue, tandem-reviewed, umbrella link guaranteed, template fallback on refusal/meta/slop.
- **More regex scrubbers** — IPv6 (timestamp/`::`-scope safe), GitLab/Slack/npm/Google/Stripe tokens, SSH public keys, `user:pass@` in http/git/ftp URLs.
- GUI: engine picker gains Tandem; Auto is labeled for what it now does.

## [2.1.0] — 2026-08-15
**Closure intelligence: stale sweeps and maintainer merges.** On 2026-08-15 two bulk-close patterns hit the filed reports: the inactivity bot closed 364 untriaged reports as "not planned" over seven daily sweeps ("inactive for too long"), and a maintainer consolidated 22 same-trigger reports into open canonicals ("Closing as a duplicate of #N"). Authors cannot reopen an issue someone else closed, so ClAudit now tracks and answers both patterns instead of pretending reopen works:
- `closure_scan` classifies every recently closed report once into swept / merged / closed (`state['__closures__']`), windowed by update date like the other sweeps.
- `defend_swept` answers each swept report: it attempts the reopen, and when GitHub refuses it posts one still-relevant note pointing at the umbrella issue (#86940 by default, config `umbrella_issue`) that collects the swept reports with their request IDs. `update_umbrella` keeps that issue's body current, grouped by sweep day.
- `fold_merged` accepts maintainer consolidation as fair triage and folds the closed duplicates' request IDs onto their open canonical (one comment per new batch, plus the 👍 the close comment asks for), so no blocked call becomes untraceable.
- All three are idempotent twice over: state plus comment-marker re-parsing, so a lost state file cannot double-post (the markers also recognize the manual 2026-08-15 defense run).
- CLI: `--sweep-scan` (classify + rollup, posts nothing), `--defend-closures`, `--update-umbrella`, and `--closures` / `--closure-interval` for the watch loop.
- GUI: closure defender runs by default (15-min cadence, toggle in tray + Settings), rows show 🧹 swept / ⇥ merged / 📎 canonical with sweep date, defense status, and merge target in the tooltip; the stats bar counts swept and merged; the board filter gains "Swept" and "Merged"; the detail timeline names sweeps, merges, folds, and umbrella notes; right-click jumps from a merged report to its canonical and from a swept one to the umbrella.
- Fix: `closure_info` (and the new classifier) no longer rely on `gh issue view --json stateReason`, which older gh builds reject; that failure was silently skipping every close in `reopen_dupe_closes`. Both now read the REST `state_reason` / `closed_by`.

## [2.0.110] — 2026-07-07
**Survive GraphQL rate-limit exhaustion.** A heavy day (backfill + repeated full sweeps) spent the account's 5000/hr GraphQL budget mid-run; 29 issues became unverifiable and the cloud run failed (the gate worked, but the defense should outlast the budget, not just report it):
- Issue fetches in the defender, verifier, and closure checks now detect exhaustion, **sleep until the budget resets, and retry** — a guarantee like "every issue checked daily" stays honest instead of quietly skipping.
- `reopen_dupe_closes` now windows to recently-updated closes (`since_days=7` default) — the stateless cloud runner was re-walking every old close each pass (~450 wasted calls/run). `--reopen-dupes` CLI still does a full backfill.
- The verifier logs unverifiable issues distinctly from confirmed-unanswered ones.

## [2.0.109] — 2026-07-07
- **Fix: tray pill stuck at 600.** The open-alerts badge counted open issues in the fetched list, which is capped by GitHub search (was `--limit 600`) — with 800+ open issues the pill froze at the cap. The count now comes from the search API's exact `total_count` (open ClAudit-filed minus harness), with the sample count as fallback; the community list cap is raised to 1000.

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

## [2.0.1 – 2.0.32] — 2026-06-25
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

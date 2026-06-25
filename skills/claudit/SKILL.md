---
name: claudit
description: Scan this machine's Claude Code sessions for false-positive safety / Usage-Policy / auto-mode-classifier blocks and file them as clean, PII-scrubbed GitHub issues on anthropics/claude-code. Use when the user wants to review, queue, or report the legitimate-but-blocked requests Claude Code has refused — or to vote in the community poll.
---

# ClAudit — report false-positive Claude Code blocks

ClAudit ([github.com/sworrl/ClAudit](https://github.com/sworrl/ClAudit)) turns the server-side blocks
Claude Code hits during legitimate work into clean, deduplicated, **PII-scrubbed** GitHub issues so
Anthropic can actually see and fix them. This skill drives its CLI (`claudit_scan.py`, also installed
as `claudit-watch`).

## Before you do anything

Filing posts **public** GitHub issues under the user's own GitHub identity. Treat that as an
outward-facing action: **show the user what would be filed and get explicit confirmation before any
command that posts.** Read-only/dry-run commands are safe to run without asking.

Requirements: the `gh` CLI authenticated (`gh auth status`), and `python3`. Burn-tokens / gate modes
also need the `claude` CLI on PATH.

## How to use it

Pick the command that matches the intent. Run from a ClAudit checkout, or use the installed
`claudit-watch` entry point.

### 1. Review — what would be reported (safe, no posting)
```
python3 claudit_scan.py            # dry-run: list NEW false-positive findings, file nothing
python3 claudit_scan.py --pending  # list blocks the background watcher has queued
```
Summarize the findings for the user (kind, project, the triggering prompt, Request IDs). Never paste
raw transcript text you haven't confirmed is scrubbed.

### 2. File the backlog (POSTS — confirm first)
```
python3 claudit_scan.py --post              # review each in $EDITOR, then file
python3 claudit_scan.py --post --no-review   # file without the editor step
```
Add `--burn-tokens` to have Claude write each report (strongest PII defense), or `--limit N` to cap
how many are filed this run.

### 3. Watch continuously
```
python3 claudit_scan.py --watch --auto --backfill   # auto-file new blocks + drip the backlog
```
Mention that the **GUI** (`claudit-gui`) is the friendlier way to run this — tray app, live dashboard,
one-click poll voting.

### 4. Defend an issue wrongly flagged as a duplicate (POSTS — confirm first)
```
python3 claudit_scan.py --dedup-guard --apply
```
👎s the dup-bot and posts a factual "not a duplicate" note **only** on issues judged genuinely
distinct.

## Key principles to honor

- **PII first.** ClAudit scrubs with regex + the user's `~/.claude/claudit/scrub.txt` denylist + an
  optional LLM pass. If the user mentions org names, client names, internal hostnames, or codenames,
  suggest they add them to `scrub.txt` before filing.
- **Don't pre-judge.** ClAudit reports *every* genuine block by default; whether a block was "correct"
  vs a false positive is the contested thing it exists to surface, so it isn't filtered out. (An
  opt-in `--gate` LLM pre-filter exists but is off by default.)
- **Never sent:** rate-limit, overloaded/529, and usage-cap errors are logged, not filed.

## Community poll

Users can vote on whether Anthropic will fix the over-blocking by reacting 👍 / 👎 / 👀 on the pinned
poll issue: https://github.com/sworrl/ClAudit/issues/6 — live tally at
https://sworrl.github.io/ClAudit/ and in the app.

<div align="center">

<img src="claudit_icon.png" alt="ClAudit" width="128">

# ClAudit

**Catch false-positive Claude Code safety/policy blocks across all your sessions, scrub the PII, and file clean GitHub issues — automatically.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)

</div>

---

## What it is

If you do authorized security work with Claude Code, you've hit this: a legitimate,
in-scope request gets stopped by a server-side block —

> `API Error: Opus has safety measures that flagged this message for a cybersecurity topic.`
> `API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy.`

Each of those carries a **Request ID** that Anthropic can look up server-side — which makes
it a genuinely actionable false-positive report. ClAudit watches your Claude Code session
logs, finds those blocks, **deduplicates** them, **scrubs PII**, and files them as GitHub
issues (with the Request IDs attached) so the false positives actually get seen.

It is **not** a spam tool. It logs-and-ignores transient noise (rate limits, overloaded,
usage caps), files at most one issue per distinct blocked request, runs **single-instance**,
and defaults to **review-before-send**.

## How it works

```
~/.claude/projects/**/*.jsonl   →  scan & classify  →  dedup (by prompt)  →  scrub PII  →  gh issue
   (your session transcripts)        cyber / aup            one per finding      regex          create
```

- **Files issues for:** cybersecurity safety-filter blocks, AUP / Usage-Policy blocks.
- **Logs but never sends:** overloaded/529, rate-limit, usage-limit, and any other API error
  (written to `~/.claude/claudit/error-log.jsonl`).
- **Dedup:** findings are keyed by the triggering prompt, so retries of the same request
  collapse into one issue with all Request IDs attached. A persistent state file
  (`~/.claude/claudit/filed.json`) means reruns never double-post.
- **Single-instance:** a PID lockfile guarantees only one watcher runs — no races, no doubles.
- **Conversation leadup:** the few turns *before* each block are captured, PII-scrubbed, and
  included in the issue (and a local `~/.claude/claudit/issues.jsonl` database of everything filed).
- **Backfill:** drip-file your existing backlog slowly (one issue every N minutes) *while* the
  watcher keeps insta-posting genuinely new blocks — so you catch up without tripping spam limits.

## Install

```bash
git clone https://github.com/sworrl/ClAudit.git
cd ClAudit
pip install -r requirements.txt        # PyQt6 (GUI) + Pillow (icon regen)
gh auth login                          # GitHub CLI must be authenticated
```

Requirements: **Python 3.9+**, the **[`gh`](https://cli.github.com/) CLI** (authenticated).
The GUI needs **PyQt6**. Desktop toasts use `notify-send` on Linux, `osascript` on macOS, and
PowerShell on Windows (best-effort; the GUI's tray notifications are cross-platform).

## Usage

### GUI (recommended) — tray icon + issue dashboard

```bash
python3 claudit_gui.py                 # notify-only: queues blocks, you click "Report pending"
python3 claudit_gui.py --auto          # auto-file new blocks as they appear
```

A system-tray icon (native StatusNotifier — works on KDE/GNOME/Windows/macOS) plus a window
listing **queued** and **reported** issues with their live GitHub **open/closed** status.
Double-click any row to see its Request IDs, block message, and a link. Closing the window
keeps it watching in the tray. Toggle auto-post from the tray menu.

### CLI — headless watcher

```bash
python3 claudit_scan.py --baseline     # mark existing blocks as seen (run once; files nothing)
python3 claudit_scan.py --watch        # notify-only: detect + queue new blocks
python3 claudit_scan.py --watch --auto # auto-file new blocks
python3 claudit_scan.py --watch --auto --backfill   # also drip-file the old backlog slowly
python3 claudit_scan.py --pending      # list what's queued
python3 claudit_scan.py --file-pending # file the queue (user-initiated)
python3 claudit_scan.py --post         # one-shot: review the backlog in $EDITOR, then file
```

| Flag | Meaning |
|------|---------|
| `--watch` | Poll forever (default: notify-only) |
| `--auto` | With `--watch`: auto-file instead of queuing |
| `--backfill` | With `--watch`: slowly drip-file the baselined backlog (`--backfill-interval N` min, default 10) |
| `--baseline` | Mark all current findings seen, file nothing |
| `--pending` / `--file-pending` | List / file the queue |
| `--post` | Review backlog in `$EDITOR` then file |
| `-R owner/repo` | Target repo (default `anthropics/claude-code`) |
| `--interval N` | Poll seconds (default 30) · `--delay N` between posts (default 3) |

### Manual one-off — paste an issue, scrub it, file it

```bash
python3 claudit.py                     # paste text, Ctrl-D; scrubs PII, opens $EDITOR, files
python3 claudit.py -f notes.md         # or from a file / -c for clipboard
```

## PII scrubbing

Before anything is posted, ClAudit redacts: API keys (Anthropic/OpenAI/AWS), GitHub & Bearer
tokens, JWTs, emails, home-directory usernames, UUIDs, IPv4s, phone numbers, and exemption-link
tokens. Request IDs are **kept** (they're the actionable part). Issues also include a best-effort,
PII-scrubbed **work-context** line (e.g. `/…/Documents/GitHub/argus`, username stripped) so
maintainers see the domain without leaking who you are. The scrubber is a safety net, not a
guarantee — the `--post` and notify-only flows let you eyeball before sending.

## Autostart

- **Linux:** `./scripts/install-linux.sh` (add `--autostart` to start on login).
- **Windows:** put a shortcut to `pythonw claudit_gui.py` in `shell:startup`.
- **macOS:** add `claudit_gui.py` as a Login Item.

## Responsible use

ClAudit posts to a public repository under **your** GitHub identity. Please:

- Only report blocks on **genuinely in-scope, authorized** work. The default issue text states
  exactly that — don't file things that aren't.
- Keep **auto-post off** unless you trust the signal; review mode exists for a reason.
- Don't disguise duplicates to evade GitHub's dedup — fix the dedup instead (ClAudit already does).
- This is feedback tooling, not a megaphone. Quality over volume.

## Project layout

| File | Purpose |
|------|---------|
| `claudit_scan.py` | Watcher: scan, classify, dedup, file/queue, single-instance lock |
| `claudit_gui.py` | PyQt6 tray app + issue dashboard |
| `claudit.py` | Manual paste → scrub → file, and the shared PII scrubber |
| `scripts/gen-icon.py` | Regenerate `claudit_icon.png` |
| `scripts/install-linux.sh` | Install desktop launcher / autostart |

## License

[MIT](LICENSE) © 2026 sworrl

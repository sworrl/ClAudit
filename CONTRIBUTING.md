# Contributing to ClAudit

Thanks for looking. The goal is to make false-positive reporting less painful for everyone using
Claude Code, and to keep the reports credible while doing it.

## Where help is useful

- Run it on Windows or macOS and say what happened. The tray, the notifications, the dashboard, the
  autostart installers. Issue [#1](https://github.com/sworrl/ClAudit/issues/1) is the place.
- New block signatures. If Claude Code stops you with a phrasing ClAudit does not classify, open an
  issue with the "Block signature" template: the scrubbed error text and a Request ID is enough.
- Better PII scrubbing. Add a pattern to `SCRUBBERS` in `claudit.py` with a test.
- Packaging. `packaging/` has a Homebrew head formula and an Arch PKGBUILD that nobody has tried on
  a clean machine yet. Flatpak is untouched.

## Dev setup

```bash
git clone https://github.com/sworrl/ClAudit.git
cd ClAudit
pip install -r requirements.txt
pip install ruff pytest
gh auth login
python3 claudit_scan.py --doctor   # every prerequisite, one line each
python3 claudit_scan.py            # dry run: reads ~/.claude/projects, posts nothing
```

Nothing posts to GitHub unless you pass `--auto`, `--file-pending`, `--post`, or click something
in the GUI, so it is safe to hack on. State lives in `~/.claude/claudit/`; delete it to reset.

## Pull requests

- Keep changes focused and match the existing style: stdlib first, no heavy dependencies in the
  core. `claudit_scan.py` and `claudit.py` have to stay importable without PyQt6.
- CI runs `py_compile`, `ruff check --select E9,F63,F7,F82`, and `pytest tests/` on Linux (Python
  3.9, 3.12, 3.13), macOS, and Windows, then builds the wheel and smoke-tests the console scripts.
  Run the fast part locally before pushing:

  ```bash
  ruff check --select E9,F63,F7,F82 . && python -m pytest tests/ -q
  ```

- Add a test for any behavior change. The suite mocks `gh`, `claude`, and `agy` and never touches
  the network.
- Bump `__version__` in `claudit_scan.py` on every code change and add a `CHANGELOG.md` section
  for it. CI fails if the README version line or the changelog disagrees with `__version__`. The
  pre-commit hook keeps the README line in step and bumps the patch number when you forget:
  `git config core.hooksPath scripts/githooks`.
- Python 3.9 is the floor. No `match`, no `X | Y` unions, nothing newer than 3.9.
- Published text (README, issue comments ClAudit posts, docs) is plain and factual. No emoji, no
  em-dashes, no brochure wording. If a sentence could sit on any product page, rewrite it.

## Releasing

Releases are tag-driven. Set `__version__`, add the changelog section, commit, then:

```bash
git tag v2.6.0 && git push origin main v2.6.0
```

`.github/workflows/release.yml` checks the tag against `__version__`, builds the sdist and wheel,
takes that version's changelog section as the notes, and publishes the GitHub Release.

## Ground rules

ClAudit reports legitimate, in-scope false positives. Do not add anything designed to spam a repo,
evade duplicate detection, or post content that is not a genuine false positive. Quality over volume.

By contributing you agree your work is licensed under the project's [GPL-3.0](LICENSE).

# Round 4: the stranger's first hour

Scope: what a person gets who types `pip install standupguy` and follows the
README. Branch `expert-2`. Nothing was published, tagged, pushed or sent.

## Honesty first

- **No independent reviewers were spawned.** This round was run by one agent
  playing each specialist in turn (no sub-agent tool was available). The
  findings below are real commands with real output, but they are one
  person's panel, not five. A human second look is still worth doing.
- **Windows was not run.** The Windows review is static (reading the code for
  path, encoding, signal and file-lock problems). CI runs the suite on Windows
  but that has not been seen either.
- Python 3.11 and 3.14 were not run; 3.10, 3.12 and 3.13 were.
- macOS was not run.

## Baseline (before any change)

| | |
|---|---|
| Tests | `python3 -m pytest -q`: 546 passed, 666 subtests, 4 min 25 s on a busy machine |
| Benchmark | `python bench/bench.py`: results.json unchanged; grouping 96%, refs 100/100, noise 16/0, secrets leaked 0/120, false redactions 0/41; about 30,000 subjects/s |
| Lint | `ruff check --select F,E9`: 0 in the package, 3 in tests |
| Registry | `pip index versions standupguy`: **0.1.0** is the only published version |

## Specialists and what each found

1. **First-time user (README only, clean venv, Python 3.10/3.12/3.13).**
   Built the wheel, installed it non-editable in fresh venvs from a folder
   with a space in its name, ran every README quick-start line
   (`--week`, `--sprint`, `--stat`, `--format json|slack|plain`,
   `--style ytb`, `--group ticket`, `--redact strict`, `--draft --dry-run`,
   `--push-slack --dry-run`, `mcp`, `serve`). All worked. Two problems:
   (a) with a git e-mail that differs from the one on the commits the
   digest is empty with "Touch grass" and no reason; (b) the README said
   0.2.0 was unpublished, but PyPI has 0.1.0, so `pip install standupguy`
   gives a tool without most of what the README describes.
   Proof: `pip index versions standupguy`; `standupguy --author nobody@x.com`.
2. **Release / packaging engineer.** `python -m build` then
   `twine check --strict`: both pass. Wheel holds only `standupguy/*.py`,
   LICENSE, METADATA, entry point; the sdist adds tests, bench, workflow,
   CHANGELOG (intended: the tests run from the sdist). Grep of both for
   home paths and token shapes: none. Found: no `__version__` (version typed in
   `cli.py` and again in `mcp.py`), no author, no Source/Changelog URL, no
   `py.typed` (see idea 10). Proof: `python -c "import standupguy;
   standupguy.__version__"` raised AttributeError.
3. **Supply-chain reviewer.** Zero runtime dependencies; build backend is
   setuptools>=77 only; CI workflow has `permissions: contents: read`. Open
   point: `actions/checkout@v4` etc. are tags, not commit hashes (idea 22,
   not done: needs network lookups of hashes). No install-time code (no
   `setup.py` hooks).
4. **Hostile-input fuzzer.** Commit with an OSC title sequence, a bidi
   override, a markdown link, `@channel` and a planted token (built at run
   time): all shown as visible escapes or redacted. `serve`: wrong Host
   header 403, 3 MB POST 501, 100 KB query 414, 30 parallel requests all 200.
   Found: a `PATH` entry that is a file made git lookup raise
   `NotADirectoryError` (a traceback). Proof:
   `PATH=/dev/null python -m standupguy doctor` before the fix.
5. **Docs/DX writer.** The plain empty-state and error messages are good. Found
   that a backwards range (`--since 2026-10-15 --until 2026-10-14`) gives a
   friendly "no commits" instead of an error, and that the Windows section said
   "Works on Windows 11" although it was never run there.
6. **CI/CD integrator.** The package job installs the wheel and runs
   `--version`; it did not run anything that touches git. Added
   `standupguy doctor` to it.
7. **Performance engineer.** 20,000 commits in 2 days: 10 s wall, 1.9 s CPU
   (time is git and a loaded machine), JSON 9 MB. No cap exists (idea 14, not
   done). `--scan` over a folder holding many repos printed one hint per quiet
   repo in an early draft of the new hint; fixed in Pass B.
8. **Competitor's maintainer.** Downloaded the wheels of standup-ai 0.2.0 and
   standupbrain 1.0.4 (both still the newest on PyPI) and read them: the README
   comparison still holds. Their GitHub pages and git-standup were not
   re-read (unverified).

## Ideas, scored

User value / differentiation / effort / risk, H M L.

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | `standupguy doctor` (git, Python, encoding, git e-mail) | H | M | L | L | DONE |
| 2 | One `__version__` for CLI, MCP and pyproject, test-guarded | M | L | L | L | DONE |
| 3 | Say why a digest is empty when only the author filter hid commits | H | M | L | L | DONE |
| 4 | Backwards plain-date range is an error | M | L | L | L | DONE |
| 5 | `PATH` entry that is a file / git not runnable: no traceback | M | L | L | L | DONE |
| 6 | Windows case-insensitive path compare (scan, worktrees, sessions) | M | L | L | L | DONE |
| 7 | Metadata: author, Source, Changelog URLs | M | L | L | L | DONE |
| 8 | Lint config (F, E9) and clean tests | L | L | L | L | DONE |
| 9 | README: correct PyPI status, Windows claim, `doctor` | H | L | L | L | DONE |
| 10 | `py.typed` marker | L | L | L | M | NOT-DONE: no type checker is configured, so the marker would promise something untested |
| 11 | `--since garbage` as a hard error | M | L | L | M | NOT-DONE: breaks the documented warning behaviour |
| 12 | Shell completion | L | L | M | L | NOT-DONE |
| 13 | Config file | M | L | M | L | NOT-DONE: `tomllib` needs Python 3.11, 3.10 is supported |
| 14 | Cap / notice for 20,000-line digests | L | L | L | L | NOT-DONE |
| 15 | Publish workflow (trusted publishing) | M | L | M | H | NOT-DONE: Dan's decision, it is publishing automation |
| 16 | Drop tests from the sdist | L | L | L | M | NOT-DONE: they are there so the sdist can be tested |
| 17 | `git fetch` first | M | L | L | M | NOT-DONE: network, changes the repo |
| 18 | Remote-only branches | M | L | M | M | NOT-DONE |
| 19 | Per-author sections for `--all` | M | M | M | L | NOT-DONE |
| 20 | `pipx` install line | L | L | L | L | NOT-DONE: not tested here |
| 21 | Python 3.14 classifier | L | L | L | L | NOT-DONE: CI lists it, never seen passing |
| 22 | Pin Actions to commit hashes | M | L | L | L | NOT-DONE: needs network lookups |
| 23 | GitHub / Jira lookups | M | M | H | H | NOT-DONE: live services, tokens |
| 24 | Built-in holiday calendar | M | L | H | H | NOT-DONE: would be invented data |
| 25 | Per-repo hint under `--scan` | L | L | L | L | DONE as "stay quiet" in Pass B |

## Pass B: attacking the new code

- New hint in a `--scan` run printed one line per quiet repo. Fixed: silent
  under `--scan`; with several `--root` it names the repo. Tests added.
- Rebuilt sdist and wheel from the final tree, `twine check --strict` passed,
  reinstalled in fresh 3.10, 3.12 and 3.13 venvs, ran `--version`, `doctor`,
  the default digest and `-I` import of `__version__` from a folder with a
  space: same output on all three.
- Re-ran every README quick-start line from the built wheel (list in item 1).

## Windows static review (not run)

Checked: `shell=True` (none), `signal` use (none), `os.replace` / file
renames (none in the package), `os.fork`/`fcntl` (none), CRLF (git output
decoded as bytes, split on `\n` only), cp1252 console (stdout set to replace,
tested by Linux stand-in), path separators (git paths come back with `/`;
only compared with `realpath`), case-insensitive volumes (fixed: three places
now `normcase`). Open: git's `worktree list` prints `C:/x` while `realpath`
gives `C:\x`; `realpath` on both sides makes them equal, but this was not seen
on Windows.

## What could not be tested

Windows, macOS, Python 3.11 and 3.14, a real PyPI upload, a real Slack
webhook, a real model endpoint, git older than 2.43, GitHub pages of the
competitors. No money was spent; nothing called a paid service.

## Numbers

| | before | after |
|---|---|---|
| Tests | 546 passed | 559 passed (+13 new), 666 subtests |
| Benchmark | as committed | unchanged (nothing in the measured path changed) |

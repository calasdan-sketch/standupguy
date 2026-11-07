# Changelog

## 0.3.0 - unreleased

Measured by `python bench/bench.py` (see the README, *What changed in 0.3.0*).
PyPI has 0.1.0 (read-only `pip index versions standupguy`): the next release
is 0.3.0, which also carries everything listed under 0.2.0 below.

### Added (round 4: the first hour of a stranger who installs it)
- `standupguy doctor`: git found and new enough, Python, output encoding, the
  git e-mail the digest will filter by. One plain line each; exit 2 only if
  git is missing or too old. Sends nothing.
- `standupguy.__version__`; `--version`, the MCP server's `serverInfo` and
  `pyproject.toml` now come from one place and a test keeps the CHANGELOG
  heading in step.
- When the digest has no commits only because of the author filter (your git
  e-mail is not the one on your commits, a common first run), a `note:` on
  stderr says how many commits other authors made in the window and points at
  `--all` / `--author`. Output and `--format json` are unchanged.
- Package metadata: author (Calas Automations Inc.), Source and Changelog
  URLs.

### Fixed (round 4)
- `--since 2026-10-15 --until 2026-10-14` (two plain dates, backwards) was an
  empty digest with a cheerful message; it is now `error:` and exit 2.
- A `PATH` entry that is a file, or a `git` that may not be run, was a
  traceback (`NotADirectoryError`, `PermissionError`); it is now the usual
  `git not found on PATH` line.
- Windows: `--scan` / `--root` duplicates, linked worktrees and Claude Code
  session folders were compared case-sensitively; now `os.path.normcase`d
  (not run on Windows).
- README: it said 0.2.0 was unpublished; PyPI has 0.1.0. The Windows section
  claimed "works on Windows 11"; it now says it was not run there.
- Lint: unused imports and a variable in the tests; a `ruff` config
  (`F`, `E9`) so the check is the same for everyone.

### Added (round 3: expert review of the 0.3.0 line)
- `--scan DIR` (repeatable) and `--scan-depth N`: digest every git repo under
  a folder; quiet repos are left out. Never follows a symbolic link, skips
  dependency and build folders, never looks inside a repo, refuses (does not
  truncate) more than 100 repos or 20,000 folders.
- `--stat`: lines added/removed on each commit line and in JSON (`added`,
  `deleted`, only when asked). Binary files count for neither.
- `--holidays 2026-12-25,...` / `STANDUPGUY_HOLIDAYS`: days "last workday"
  skips. No holiday calendar is built in. Strict `YYYY-MM-DD`.
- `serve`: `redact=off|standard|strict` makes the JSON `commits` (subjects,
  file names, e-mails), the Markdown and the Slack text follow one profile.
  Default unchanged (JSON exact).
- `bench/`: `tag_repeats` (lines whose ticket tag repeated the text: 7 of 12
  on the hand-labelled subjects and 610 of 972 generated ones before, 0 after).

### Security (round 3)
- Every git call forces off `core.fsmonitor` (a command git RUNS on
  `git status`: a repo config could execute anything), `log.showSignature`
  (runs `gpg.program`), colour, decoration, path quoting, output encoding;
  sets `GIT_OPTIONAL_LOCKS=0` (reading no longer takes the index lock or
  rewrites the index); drops `GIT_DIR`, `GIT_WORK_TREE` and similar
  (inside a git hook they made `--root` read the wrong repo).
- `git status` runs a file's `clean`/`process` filter from the repo's own
  config. Filters defined in the repo's local/worktree config are switched
  off (system and global ones, git-lfs, stay). Needs git 2.26 for
  `--show-scope`; otherwise a warning and no "In progress" list.
  `--ignore-submodules=dirty` keeps git from starting inside submodules.
- A failing `git status` (a broken index, say) was a traceback; it is now a
  one-line warning and the rest of the digest.

### Fixed (round 3)
- `add login [ABC-12] [ABC-12]`: a ticket or issue the line already names is
  not tagged again.
- A one-megabyte commit subject made a one-megabyte line: digest lines are cut
  at 300 characters, after redaction (a secret is never cut in half). JSON
  and `serve` JSON stay whole.
- An empty commit message printed `-  (1 file)`; it reads `(no message)`.
- Agent detection ran seven regexes three times per commit; a one-pattern
  pre-check skips the commits that name no agent (about 15% less CPU on
  20,000 commits).
- Test helper in `tests/test_round7.py` finds git's sub-command past the
  forced `-c` settings (the command line shape changed).

### Considered, not built (round 3)
Scored value / differentiation / effort / risk (H, M, L):
GitHub or Jira lookups (M/M/H/H: tokens, live services, breaks "offline");
`git fetch` first (L/L/L/M: network, changes the repo); remote-only branches
(M/L/M/M: stale without a fetch); config file (M/L/M/L: `tomllib` needs
Python 3.11, we support 3.10); lower-case ticket keys (L/L/L/H: `utf-8`);
"since last run" state file (M/M/M/M: hidden state, surprising windows);
HTML or PDF output (L/L/M/L); per-author sections for `--all` (M/M/M/L);
`--tz` (L/L/M/M: the committer date's day is already local); a built-in
holiday calendar (M/L/H/H: would be invented data); `--max-commits` cap with
a notice (L/L/L/L: window sizes are small in practice); running git in a
sandbox (M/M/H/M); localised headings (M/L/M/L); time-spent estimates from
commit times (L/L/L/H: misleading); Slack Block Kit output (L/L/M/L).

### Added
- **References.** `ABC-123`, `#77`, `GH-77`, `closes #77`, squash `(#412)`,
  merge and Bitbucket/GitLab pull and merge requests, from the subject and the
  body, shown as `[ABC-123] [#77] [PR #412]`. `--group ticket`. JSON: `refs`.
- **Grouping by what a commit did**, not only by `feat:`: first verb,
  gitmoji, merge-branch names, touched files. JSON: `group`.
- **Folding** of `fixup!`/`squash!`, bare WIP saves, branch syncs, revert
  pairs and same-day duplicates, with a counted *Folded away* line.
  `--no-fold`.
- **Redaction** (`--redact off|standard|strict`, `--redact-term`,
  `STANDUPGUY_REDACT`): secrets, URL queries, internal hosts, e-mails,
  private IPs; `strict` also links and repo names. Default `standard`.
- **Markdown escaping** of links, autolinks/HTML, mentions and unbalanced
  backticks; mass mentions become `[at]channel`.
- **Other work**: other branches (pushed or local only), stashes, worktrees,
  commits only the reflog holds. `--no-inflight`. JSON: `in_flight`.
- **Mailmap**: the log uses `%aN`/`%aE`.
- **Agents**: commits made with Claude, Copilot, Codex, Cursor, Devin, Gemini
  or Aider are flagged; `--agent-sessions` (opt-in) counts local Claude Code
  sessions. JSON: `agent`.
- `--format plain|slack|teams`, `--copy`, `--style ytb`.
- `bench/`: the benchmark, its hand-labelled subjects and committed results.

### Changed
- Markdown output is redacted and escaped by default (`--redact off` for the
  old text). `serve` and `mcp` redact but do not escape.
- `(#412)` at the end of a subject is a `[PR #412]` tag, not line text.
- Version `0.3.0`.

### Security
- A misspelt `STANDUPGUY_REDACT` is an error (exit 2), never a quiet fall-back.
- `--copy` puts the same visible escapes on the clipboard as on the terminal.
- Every redaction pattern is bounded; a one-megabyte subject of one repeated
  character takes about a tenth of a second (an early draft took 30 seconds).
- Folding is linear in the number of commits.

## 0.2.0 - unreleased

### Changed
- **The default `--since` is now `last workday`**: Friday on a Monday or a
  weekend, yesterday on Tuesday to Friday, so Monday's standup includes the
  weekend instead of coming back empty. The heading names the day it
  picked (`_since last workday (Fri 2026-10-16)_`). The window is always the
  same as, or larger than, the old `yesterday` one; `--since yesterday`
  gives the old behaviour. `serve` (`/digest`) and the MCP tool use the
  same default.
- `--format json` and `serve` commits carry a new key, `coauthors` (a list,
  empty when there are none). Readers that ignore unknown keys are not
  affected.
- `(1 file)`, not `(1 files)`.

### Added
- `--workdays DAYS` / `STANDUPGUY_WORKDAYS` (`mon-fri` by default, `sun-thu`,
  `mon,wed,fri`, wrapping ranges). A value that names no weekday is an
  error (exit 2), never a quiet fall-back.
- `last workday` as a date value everywhere (`--since`, `--until`, `serve`,
  MCP).
- `Co-authored-by:` trailers match `--author`: work you paired on is in your
  digest. At most 20 co-authors per commit, 200 characters each.
- `--dry-run` with `--draft` prints the endpoint (no login or query string),
  the model, whether an API key is set (never the key) and the exact prompt;
  with `--push-slack` it prints the exact Slack text. Nothing is sent.
  `--dry-run` alone is an error (exit 2).

### Security
- Bidi controls (Trojan Source), zero-width characters, the BOM, line and
  paragraph separators and Unicode tag characters (U+E0000-U+E007F, used for
  invisible prompt injection) in commit text are shown as visible escapes
  on the terminal, in Slack text (`--push-slack`, `serve`'s `slack_text`)
  and in the `--draft` prompt. Zero-width joiner and non-joiner are kept
  (emoji, Persian and Indic text need them).

### Fixed after the panel review
- Variation selectors (U+FE00-FE0D, U+E0100-E01EF), the soft hyphen,
  U+034F, U+17B4/U+17B5 and U+1D173-U+1D17A are escaped too: bytes hidden
  as variation selectors no longer reach the terminal, Slack or the model.
  One VS15/VS16 right after a visible character (an emoji's) is kept on the
  terminal and in Slack; the model prompt escapes all of them. The README
  no longer claims more than the list covers.
- A whole e-mail address given to `--author` (and the default
  `user.email`) matches the author's or a co-author's address exactly:
  `dan@x.com` no longer selects `jordan@x.com`. Partial needles are still
  substrings. README note on GitHub noreply addresses in trailers.
- A newline in a file name, the repo name or a label is shown as `\n` in
  the Markdown, and the Slack sanitizer splits lines on `\n` only (it used
  to split on \x0b, \x0c, \x1c-\x1e, \x85, U+2028/9 too). The `--draft`
  prompt fences the digest between marker lines with a per-prompt random
  token and tells the model the digest is data, not instructions.
- `--dry-run` shows only the usual API words of the endpoint path (`v1`,
  `api`, `openai` ...); any other part is `***`.
- A blank `STANDUPGUY_WORKDAYS` counts as not set, like an empty one (it
  was an error); an empty or blank `--workdays` is still an error.

### Docs
- README: a one-line summary, a demo at the top, a comparison table with
  links to each tool's own page (Geekbot: $2.50 yearly, $3 monthly), and corrected limits (the files use LF line
  endings; CI runs on Windows and macOS too). The unsourced "2/5 of the
  week" claim was removed.

## 0.1.3
- YYYYMMDD dates, a slow git and a closed stdin end in one clear line, no
  false weekday warning, the README says which branch the digest reads.

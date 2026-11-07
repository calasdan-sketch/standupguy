# Changelog

## 0.3.0 - unreleased

Measured by `python bench/bench.py` (see the README, *What changed in 0.3.0*).

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

# Changelog

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

### Docs
- README: a one-line summary, a demo at the top, a comparison table with
  links to each tool's own page, and corrected limits (the files use LF line
  endings; CI runs on Windows and macOS too). The unsourced "2/5 of the
  week" claim was removed.

## 0.1.3
- YYYYMMDD dates, a slow git and a closed stdin end in one clear line, no
  false weekday warning, the README says which branch the digest reads.

# StandupGuy (`standupguy`)

Kill the tracker trap without leaking data.

Engineers lose up to 2/5 of the week to reporting busywork — duplicating
updates across Jira, Slack, Confluence, and slide decks. AI note-takers are
cloud SaaS with data-leak concerns. `standupguy` is the local-first
alternative: it reads your **git history** (ground truth) and drafts your
standup notes, weekly summary, and sprint-review bullets as Markdown.
**Your history never leaves your laptop** — except to the model endpoint
*you* configure (Ollama on localhost by default).

## Install

Zero dependencies — Python 3.10+ stdlib only. Needs `git` on your PATH
(on Windows: [Git for Windows](https://git-scm.com/download/win)).

```bash
pip install git+https://github.com/calasdan-sketch/standupguy
standupguy --version
```

That installs the `standupguy` command (`py -m standupguy` / `python -m
standupguy` works too). From a checkout: `pip install .`

## Usage

```bash
standupguy                          # yesterday's digest (deterministic, offline)
standupguy --week                   # last 7 days, grouped by day
standupguy --sprint                 # sprint review: last 14 days, by day, + carried over
standupguy --sprint 10              # ...or an N-day window
standupguy --sprint --since 2026-10-01 --until 2026-10-14   # ...or an exact range
standupguy --since 2026-10-01       # custom range (--until optional, inclusive)
standupguy --root ../api --root ../web   # several repos: one section per repo
standupguy --draft                  # LLM prose: Yesterday / Today / Blockers
standupguy --format json            # machine-readable digest
standupguy --author "teammate@co"   # filter (default: your git user)
standupguy --all                    # everyone
standupguy --push-slack             # POST to AUTOSTANDUP_SLACK_WEBHOOK
standupguy mcp                      # MCP server on stdio
standupguy serve                    # HTTP bridge for Activepieces / workflow tools
```

Output piped into something that stops reading early
(`standupguy --week | head -1`) ends quietly: exit code 1, no traceback.
On Windows too, where a closed pipe is not a broken-pipe error but a plain
`OSError` (errno 22) that shows up when the output is flushed. So does
`mcp` when its client goes away, and `serve` when a client hangs up
mid-response.

`--author` matches like you'd expect, not like a regex: a case-insensitive
substring of the author **name or email** (`calas`, `DAN@EXAMPLE.COM`,
`ξένος`, `"Dan Calas <dan@example.com>"` all work; unicode names are
matched case-insensitively, surrounding spaces are ignored, and `+`, `.`
or `[` in an address are literal — git's own `--author` is a case-sensitive
regex and would miss or fail on those). The default is your `git config
user.email`.

## Activepieces

`standupguy serve` exposes the digest over HTTP so Activepieces' HTTP
Request piece can pull it on a schedule:

```bash
standupguy serve --root /path/to/repo   # localhost:8765
```

- `GET /digest?since=yesterday&until=&author=&all=&format=json|markdown`
- `GET /week?since=&until=&author=&all=&format=json|markdown`
- `GET /health`

Defaults are the CLI's: `author` is the repo's `git config user.email`
(`all=1` for everyone), `since` is `yesterday` (`/week`: 7 days ago), and
the JSON `commits` have exactly the shape of `standupguy --format json`
(`sha, author, email, date, subject, files, pr, wip`) plus the rendered
`markdown` and `slack_text` — the same digest already sanitized and
escaped for Slack (see *Push* below); a workflow that posts to Slack
should send `slack_text`, never raw `markdown`. An impossible date
(`since=2026-13-45`) is a `400`, not a guess. A request that fails inside
the bridge (the repo was moved, `git` went missing) is a `500` with a
generic `internal error` body: git's command line and your paths are not
sent to the client, they are printed on the server's stderr, one line per
failure.

Several repos: `standupguy serve --root /path/a --root /path/b` answers for
all of them, the way `--root A --root B` does for the digest. The markdown
has one section per repo, in the order given; the JSON becomes
`{"repos": [...], "markdown": ..., "slack_text": ...}`, each entry shaped
like `standupguy --format json` (one root keeps the flat object above, so
a workflow that reads `slack_text` keeps working when you add a repo).
Each repo's default `author` is its own `git config user.email`. A port
that is already in use, or a `--root` that is not a git repo, is a
one-line `error:` and exit code 2; the server does not start.

[`workflows/activepieces-standup-digest.json`](workflows/activepieces-standup-digest.json)
is a documented flow definition (it's a human-readable spec, not a native
export — build it in Activepieces by adding the steps in order; the file
carries the exact config for each step and setup notes at the top):
weekday 9am Schedule trigger → HTTP Request → Branch (skip quiet days) →
Slack. Connect your Slack connection, set the channel, done. The same
`serve` endpoints work from n8n's HTTP Request node too — nothing in the
bridge is platform-specific.

Example output:

```markdown
# Standup — myrepo
_since 2026-10-14_

## Shipped
### Features
- add login flow (1 files) `6785c87`
### Fixes
- null pointer on logout (1 files) `8ce2731`

## In progress (uncommitted)
- `wip.py`

## Blockers
_none reported — fill in yours_
```

## How it works

- **Digest mode (default):** pure Python, fully offline. Commits grouped by
  conventional-commit prefix (feat/fix/docs/refactor/test/chore); merge
  commits surface as shipped PRs; `git status` feeds the In-progress section.
- **Commit text is untrusted.** A subject, an author name or even a file
  name can contain any control character, so the git log is read with a
  random separator picked on every run (no commit can contain it) and each
  sha is checked against `git rev-list` for the same range: a hostile
  commit cannot add a fake "shipped" entry, borrow a colleague's sha or
  rewrite another commit's author and date. A record git cannot give a
  date (a corrupt author line) is dropped rather than shown undated.
  Text printed to a terminal (the digest, a `--draft` reply) shows every
  control character as a visible escape — `\x1b` for ESC, `\x0d` for a
  carriage return, the same for the C1 range — so a commit cannot clear
  your screen, retitle the window or overwrite a line. `--format json`,
  `serve` and the MCP server are not terminals and carry the text exactly.
- **Sprint mode (`--sprint [DAYS]`):** the last DAYS days (default 14), or
  `--since`/`--until`, grouped by day with the same prefix buckets, then a
  **Carried over** section: commits *tagged* WIP/draft (`wip: …`, `WIP …`,
  `[WIP]`, `Draft: …`, `[draft]` — they are not counted as shipped; a
  subject like "Draft the release notes" is ordinary shipped work) plus
  the uncommitted changes in your working tree. Ends with the highlights
  prompt.
- **Draft mode (`--draft`):** sends the digest to your local model via an
  OpenAI-compatible endpoint and gets back Yesterday/Today/Blockers prose.
  If the model is down, hangs up, or answers with anything but a
  completion (an HTML error page, an empty or cut-off body), you get one
  `error:` line on stderr and the plain digest on stdout. The exit code is
  still 2, even though a digest was printed, so a script can tell that the
  draft failed; nothing is posted to Slack. Configure with env vars
  (Ollama defaults shown):

```bash
export AUTOSTANDUP_BASE_URL="http://localhost:11434/v1"
export AUTOSTANDUP_MODEL="llama3.1"
# freellmapi works too — it's OpenAI-compatible:
export AUTOSTANDUP_BASE_URL="https://api.freellmapi.com/v1"
export AUTOSTANDUP_API_KEY="<your key>"
```

- **Multi-repo (`--root A --root B`):** one section per repo, in the
  order given, in every mode; `--format json` then returns
  `{"repos": [...]}` (a single `--root` keeps the flat object).
- **MCP server:** exposes `what_did_i_ship {since?, until?, author?, all?,
  root?}` so your coding agent can answer "what did I ship this week" from
  local git. The server only reads the repo(s) it was started with
  (`--root`, repeatable); a `root` argument must be one of them or a
  directory inside one — an agent cannot point it at some other repo on
  your disk. It will not start when a `--root` (default: the current
  directory) is not a git repo: one `error: not a git repo: PATH` line and
  exit code 2, the same as `serve`. The stdio wire is UTF-8 bytes
  regardless of the console code page, so a cp1252 Windows console cannot
  kill it with an author called Łukasz. A call the client got wrong
  (params or `arguments` that are not objects, a missing or unknown tool
  name, an argument of the wrong type) is JSON-RPC error `-32602`; a call
  that was fine but failed (an impossible date, a root outside the
  configured ones) comes back as an `isError` result. The server keeps
  running after either, and notifications are never answered.
- **Push:** `--push-slack` POSTs the Markdown to a Slack incoming webhook
  (URL from the flag or `AUTOSTANDUP_SLACK_WEBHOOK`). Commit-derived text
  is sanitized before posting: control characters stripped, lines
  truncated at 500 chars, `@channel`/`@here`/`@everyone` (and the
  `<!channel>` markup form) rewritten as `[at]channel`-style text, and
  Slack's markup characters `&`, `<`, `>` escaped to `&amp;`, `&lt;`,
  `&gt;` — Slack's own rule for message text. So everything a commit
  message can contribute renders as *literal* text: no `<@user>` or
  `<!subteam^…>` pings, no `<http://evil|the standup>` disguised links,
  no channel-wide mentions. Jira/Confluence push
  is intentionally not built in — paste the Markdown, keep your tokens
  out of another integration.

## Date handling

Git's date parser fills in the **current time of day** for a bare date or
a word (`--since=2026-10-14` run at 15:00 means 2026-10-14 15:00;
`yesterday` means 24 hours ago) — silently dropping that morning's
commits. For a standup you want whole days, so any `--since` value
without a time of day gets ` 00:00` appended, any `--until` value without
one gets ` 23:59:59` (**a bare `--until` date is inclusive**: `--since
2026-10-14 --until 2026-10-15` is both days), and the words are handed to
git **verbatim** — git does the date math, in its own local timezone,
exactly as if you had typed `--since="yesterday 00:00"`:

| you type | `--since` gets | `--until` gets |
|---|---|---|
| `yesterday` | `yesterday 00:00` | `yesterday 23:59:59` |
| `3 days ago`, `last monday`, `today` | `3 days ago 00:00`, … | `3 days ago 23:59:59`, … |
| `2026-10-14` | `2026-10-14 00:00` | `2026-10-14 23:59:59` |
| `2026-10-14 15:30`, `noon`, `now`, `2 hours ago`, `@1792128212` | unchanged | unchanged |

Git's parser is also *fuzzy*: it turned `2026-13-45` into October 13 and
`notadate` into "now" without a word. `standupguy` refuses an
ISO-looking date that is not a real date (exit 2) and warns on stderr
when a value contains words git does not understand, then lets git guess.

Day headers (`## 2026-10-14`) are the **local date on your machine**
(`--date=format-local`), not the committer's timezone, so a teammate's
23:30 PDT commit lands on the day you actually saw it.

## Windows

Works on Windows 11 / Python 3.11: git output is decoded as UTF-8 (paths
and authors come back raw, never `caf\303\251.py`), non-ASCII commit
subjects (or the 🌱) print on a cp1252 console without
`UnicodeEncodeError` — unencodable characters are replaced — and the MCP
server reads and writes UTF-8 bytes whatever the console code page. If
`git` is not installed you get `error: git not found on PATH` (exit 2),
not a traceback. `pip install .` works (the package is declared
explicitly; setuptools' flat-layout auto-discovery used to refuse the
repo).

## Tests

```bash
python -m unittest discover -s tests   # or: python -m pytest -q
```

No network, no model: fixtures are throwaway git repos (your global
gitconfig is never read) and the "model" and "client" are fakes on
127.0.0.1. Covers the digest, `--author` matching, `--sprint` and
`--until`, local-date headers, date validation, multi-repo output,
hostile commit text (forged records, terminal escapes), the HTTP bridge
(start-up errors, several repos, hang-ups, failing requests), bad model
answers, Slack sanitizing and escaping, the MCP server (root confinement,
invalid params, notifications, start-up check, UTF-8 wire under a cp1252
console), closed pipes (real processes, and Linux stand-ins for Windows'
errno-22 version of the error), the package metadata, a missing `git`,
the cp1252 console path, and a check that no source or doc file hides an
invisible character.

## License

MIT — see `LICENSE`.

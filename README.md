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

Zero dependencies — Python 3.10+ stdlib only.

```bash
git clone <repo> && cd standupguy
# no-install alias (run from the repo root):
alias standupguy="python3 -m standupguy"
# or: pip install .   # provides the `standupguy` console script
```

## Usage

```bash
standupguy                          # yesterday's digest (deterministic, offline)
standupguy --week                   # last 7 days, grouped by day
standupguy --sprint                 # sprint review: last 14 days, by day, + carried over
standupguy --sprint 10              # ...or an N-day window
standupguy --sprint --since 2026-10-01 --until 2026-10-14   # ...or an exact range
standupguy --since 2026-10-01       # custom range (--until optional)
standupguy --draft                  # LLM prose: Yesterday / Today / Blockers
standupguy --format json            # machine-readable digest
standupguy --author "teammate@co"   # filter (default: your git user)
standupguy --all                    # everyone
standupguy --push-slack             # POST to AUTOSTANDUP_SLACK_WEBHOOK
standupguy mcp                      # MCP server on stdio
standupguy serve                    # HTTP bridge for Activepieces / workflow tools
```

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

- `GET /digest?since=yesterday&until=&author=&format=json|markdown`
- `GET /week?author=&format=json|markdown`
- `GET /health`

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
- **Sprint mode (`--sprint [DAYS]`):** the last DAYS days (default 14), or
  `--since`/`--until`, grouped by day with the same prefix buckets, then a
  **Carried over** section: commits marked WIP/draft (`wip: …`, `[WIP]`,
  `Draft: …` — they are not counted as shipped) plus the uncommitted
  changes in your working tree. Ends with the highlights prompt.
- **Draft mode (`--draft`):** sends the digest to your local model via an
  OpenAI-compatible endpoint and gets back Yesterday/Today/Blockers prose.
  Configure with env vars (Ollama defaults shown):

```bash
export AUTOSTANDUP_BASE_URL="http://localhost:11434/v1"
export AUTOSTANDUP_MODEL="llama3.1"
# freellmapi works too — it's OpenAI-compatible:
export AUTOSTANDUP_BASE_URL="https://api.freellmapi.com/v1"
export AUTOSTANDUP_API_KEY="<your key>"
```

- **MCP server:** exposes `what_did_i_ship {since?, author?, root?}` so your
  coding agent can answer "what did I ship this week" from local git.
- **Push:** `--push-slack` POSTs the Markdown to a Slack incoming webhook
  (URL from the flag or `AUTOSTANDUP_SLACK_WEBHOOK`). Commit-derived text
  is sanitized before posting: control characters stripped, lines
  truncated at 500 chars, and `@channel`/`@here`/`@everyone` neutralized
  to `[at]channel`-style text — so a hostile commit message can never ping
  the channel or inject payloads. Jira/Confluence push
  is intentionally not built in — paste the Markdown, keep your tokens
  out of another integration.

## Date handling

Git's date parser fills in the **current time of day** for a bare date or
a word (`--since=2026-10-14` run at 15:00 means 2026-10-14 15:00;
`yesterday` means 24 hours ago) — silently dropping that morning's
commits. For a standup you want whole days, so any `--since`/`--until`
value without a time of day gets ` 00:00` appended and the words are
handed to git **verbatim** — git does the date math, in its own local
timezone, exactly as if you had typed `--since="yesterday 00:00"`:

| you type | git gets |
|---|---|
| `yesterday` | `yesterday 00:00` |
| `3 days ago`, `last monday`, `today` | `3 days ago 00:00`, … |
| `2026-10-14` | `2026-10-14 00:00` |
| `2026-10-14 15:30`, `noon`, `now`, `2 hours ago`, `@1792128212` | unchanged |

Day headers (`## 2026-10-14`) are the **local date on your machine**
(`--date=format-local`), not the committer's timezone, so a teammate's
23:30 PDT commit lands on the day you actually saw it.

## Windows

Works on Windows 11 / Python 3.11: git output is decoded as UTF-8 and
non-ASCII commit subjects (or the 🌱) print on a cp1252 console without
`UnicodeEncodeError` — unencodable characters are replaced. `pip install .`
works (the package is declared explicitly; setuptools' flat-layout
auto-discovery used to refuse the repo).

## Tests

```bash
python3 -m unittest discover -s tests   # or: python3 -m pytest -q
```

No network, no model: fixtures are throwaway git repos (your global
gitconfig is never read). Covers the digest, `--author` matching, `--sprint`
and `--until`, local-date headers, the HTTP bridge, Slack sanitizing, the
MCP server and the cp1252 console path.

## License

MIT — see `LICENSE`.

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
standupguy --week                   # last 7 days
standupguy --sprint                 # sprint-review bullets
standupguy --since 2026-10-01       # custom range
standupguy --draft                  # LLM prose: Yesterday / Today / Blockers
standupguy --format json            # machine-readable digest
standupguy --author "teammate@co"   # filter (default: your git user)
standupguy --all                    # everyone
standupguy --push-slack             # POST to AUTOSTANDUP_SLACK_WEBHOOK
standupguy mcp                      # MCP server on stdio
standupguy serve                    # HTTP bridge for Activepieces / workflow tools
```

## Activepieces

`standupguy serve` exposes the digest over HTTP so Activepieces' HTTP
Request piece can pull it on a schedule:

```bash
standupguy serve --root /path/to/repo   # localhost:8765
```

- `GET /digest?since=yesterday&author=&format=json|markdown`
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

Bare dates and words are normalized to explicit midnight (`2026-10-14` →
`2026-10-14 00:00`, `yesterday` → yesterday 00:00) because git's date parser
treats them as *noon* — silently dropping morning commits.

## Tests

```bash
python3 -m unittest discover -s tests   # 36 tests, no network, no model
```

## License

TBD

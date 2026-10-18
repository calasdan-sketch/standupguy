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
standupguy serve                    # HTTP bridge for Activepieces / workflow tools (this computer only)
standupguy serve --port 9000        # ...on another port (default 8765)
STANDUPGUY_TOKEN=... standupguy serve --host 0.0.0.0   # ...reachable from other machines (token required)
```

`--format json` prints the digest as JSON and is the finished output: it
cannot be combined with `--draft` or `--push-slack` (an `error:` line and
exit code 2, before git is run or anything is sent). Draft or post the
Markdown instead.

An option that takes a value and gets an empty one (`--push-slack "$HOOK"`
when `$HOOK` was never set) is an `error:` line and exit code 2, before git
is run or anything is sent - never a quiet fall-back to the default. That
covers `--root`, `--author`, `--since`, `--until`, `--push-slack`,
`--base-url`, `--model` and `--host`. Leave the option out to get its
default; a bare `--push-slack` posts to `AUTOSTANDUP_SLACK_WEBHOOK`. An
empty environment variable counts as not set, and so does a blank value in
a request to `serve` or in an argument of an `mcp` tool (`until=`).

If standard output is closed (`standupguy >&-`) the digest and the JSON
have nowhere to go: one `error: standard output is closed ...` line and
exit code 1, not a traceback, and `mcp` says the same before it reads a
request. `--push-slack` and `serve` do not need it and still run. `mcp`
needs standard input too: started with it closed (`standupguy mcp <&-`)
there is no client to read a request from, so it says `error: standard input
is closed, so there are no requests to read` and exits with code 2.

Output piped into something that stops reading early
(`standupguy --week | head -1`) never ends in a traceback. If the reader is
already gone when the output is written, or goes away part-way through it,
standupguy stops quietly with exit code 1. A digest that fits in the pipe is
written whole and the exit code is 0 (a shell reports the last command's
code, `head`'s here, unless you `set -o pipefail`). Windows reports a closed
pipe not as a broken-pipe error but as a plain `OSError` (errno 22) when the
output is flushed; that ends the same way. `mcp` ends quietly with exit code
1 when the pipe it answers on has been closed, and `serve` shrugs off a
client that hangs up mid-response.

The digest is the history of the branch that is checked out
(`git log HEAD`): a commit made yesterday on another branch, or one that is
not merged yet, is not in it. `--all` means every author, not every branch.
Check the branch out to see its work (`git switch feature/login`).

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

`--port N` picks another port (default 8765); `--port 0` lets the system
choose a free one, and the line `serve` prints when it starts says which.

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
that is already in use, a `--root` that is not a git repo, or a network
address without a token (below), is a one-line `error:` and exit code 2;
the server does not start.

### Who can reach it

Your commit history is private (every author, e-mail address and subject),
so by default only your own computer can ask. `serve` listens on
`127.0.0.1`, and it answers a request only when its `Host` header names the
bridge: `localhost`, `127.0.0.1` or `[::1]` on the bridge's own port.
Anything else is a `403`, so a web page you happen to open cannot read your
history by pointing its own name at your computer (DNS rebinding). A
browser's cross-site request (it carries an `Origin` header) must name an
allowed host too, and the bridge never sends CORS permission. curl, n8n
and Activepieces send no `Origin` and are not affected.

To call it from another machine (Activepieces on a server, or in Docker)
it must listen on a network address, and then it needs a token:

```bash
export STANDUPGUY_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
standupguy serve --host 0.0.0.0 --root /path/to/repo
# the caller sends:  Authorization: Bearer <the token>     (or ?token=<the token>)
```

- Every request except `/health` has to carry the token, otherwise it is a
  `401`. A token is at least 16 printable characters (stick to letters,
  digits, `-` and `_` if you use `?token=`). Prefer the environment
  variable: a `--token` argument shows up in the process list.
- `serve --host 0.0.0.0` without `STANDUPGUY_TOKEN` (or `--token`) is
  refused with exit code 2 and nothing listens. `--insecure-allow-lan`
  overrides that, with a warning on stderr: use it only on a network you
  trust completely.
- A caller that reaches the bridge by a DNS name (`host.docker.internal`,
  `devbox.lan`) gets a `403` until you allow the name:
  `--allow-host host.docker.internal` (repeatable). One rule for every name:
  `--allow-host NAME` allows that name on every port, `--allow-host
  NAME:PORT` on that port only (a `Host` with no port means port 80). A
  Host that is an IP address is accepted when the bridge listens on a
  network address.
- A forwarded port reaches the bridge under a port number of its own: the
  caller asks for `http://localhost:9999/...`, the bridge listens on 8765,
  so the `Host` header says `localhost:9999` and is refused like any other
  name you have not allowed. `--allow-host` takes this computer's own names
  too (`localhost`, `127.0.0.1` and `[::1]`, each one on its own):

  ```bash
  # the bridge runs in a container started with  docker run -p 9999:8765 ...
  # (a network address, so STANDUPGUY_TOKEN has to be set, as above)
  standupguy serve --host 0.0.0.0 --allow-host localhost:9999 --root /repo

  # the bridge runs on another machine, reached with  ssh -L 9999:127.0.0.1:8765 devbox
  standupguy serve --allow-host localhost:9999 --root /repo
  ```

  An allowed `Host` relaxes nothing else: the `Origin` rule and the token
  rule apply as before.
- The token is never printed or logged. A refused request is one line on
  stderr: the path only, never the query string.
- It is plain HTTP: keep the port off the public internet.

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
- **Folders git will not use, and bare ones:** a `--root` that is not a
  repository is `error: not a git repo: PATH` (exit code 2, for the digest,
  `mcp` and `serve` alike). When git refuses the folder for another reason,
  its own message is shown instead, on the same one line: for a repository
  owned by another user that includes the `git config --global --add
  safe.directory PATH` command that fixes it. A bare repository
  (`git clone --bare`, the copy on a server) works: it has no working tree,
  so it never has an *In progress* section, and it is named after its
  folder (`project.git` is `project`).
- **A git that does not answer:** every git command is given 60 seconds. When
  one takes longer (a repository on a network drive that went away, say) the
  digest ends with one line, `error: git log did not finish within 60
  seconds - ...`, and exit code 2, not a traceback. `mcp` answers that
  request with an `isError` result and `serve` with a `500`, and both go on.
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
- **Secrets stay where you put them.** `--draft` sends the API key to the
  endpoint you configured and nowhere else: it does not follow a redirect to
  another address (a different scheme, host or port), it stops with an
  `error:` line that says where the redirect pointed, and nothing is sent
  there. A key an HTTP header cannot carry (a line break, a curly quote) is
  refused before anything is sent. The API key, the login (`user:password@`)
  and query string of the model URL, and the Slack webhook URL are never
  shown in an error message.

## Date handling

Git's date parser fills in the **current time of day** for a bare date or
a word (`--since=2026-10-14` run at 15:00 means 2026-10-14 15:00;
`yesterday` means 24 hours ago) — silently dropping that morning's
commits. For a standup you want whole days, so any `--since` value
without a time of day starts at midnight, and any `--until` value without
one ends at 23:59:59 (**a bare `--until` date is inclusive**: `--since
2026-10-14 --until 2026-10-15` is both days).

`today`, `yesterday`, `N days ago` and `N weeks ago` are worked out by
`standupguy` as **whole local calendar days** and handed to git as an
explicit date. (Git counts them in blocks of 24 hours back from now, which
is the wrong day during the first or last hour after midnight on the day
after a clock change: at 00:10 on the Monday after the clocks went
forward, git's `yesterday` is Saturday.) Every other word is handed to git
**verbatim** — git does the date math, in its own local timezone, exactly
as if you had typed `--since="last monday 00:00"`:

| you type (today is 2026-10-17) | `--since` gets | `--until` gets |
|---|---|---|
| `yesterday` | `2026-10-16 00:00` | `2026-10-16 23:59:59` |
| `today`, `3 days ago`, `2 weeks ago` | `2026-10-17 00:00`, `2026-10-14 00:00`, `2026-10-03 00:00` | the same days, at `23:59:59` |
| `last monday`, `3 months ago` | `last monday 00:00`, … | `last monday 23:59:59`, … |
| `2026-10-14`, `20261014` | `2026-10-14 00:00` | `2026-10-14 23:59:59` |
| `2026-10-14 15:30`, `noon`, `now`, `2 hours ago`, `@1792128212`, `1792128212` | unchanged | unchanged |

A bare eight-digit number such as `20261014` is the day it says, like
`2026-10-14` (git itself takes it for *now*, which leaves the digest empty
without a word). A number of nine or more digits is a Unix timestamp, as it
is for git.

**Limit:** the words git still works out itself (weekday names, `N months
ago`, `N years ago`) can be an hour off across a clock change - or, in the
first or last hour after midnight, a day. Give an explicit date
(`--since 2026-03-08`) when the exact start matters.

**Weekday names.** Git reads a weekday name only right after `last` or a
number (`last monday`, `2 mondays ago`). Anywhere else it skips the word,
so a bare `--since monday` (also `this monday`, `monday 9am`) means *today*:
on a Saturday that leaves out the whole week. `standupguy` warns on stderr
(one line for each option), still runs the value as typed, and suggests
`last monday` or a date. There is no warning when the value also gives its
date - `monday 2026-10-12`, `Mon Oct 12`, or git's own `Sun Oct 18 09:00:00
2026 -0500`, as `git log` prints it - because git reads the date and ignores
the weekday. `serve` and `mcp` take the word the way git does, without a
warning.

Git's parser is also *fuzzy*: it turned `2026-13-45` into October 13 and
`notadate` into "now" without a word. `standupguy` refuses an
ISO-looking date that is not a real date (exit 2; so is `20261345`) and warns
on stderr when a value contains words git does not understand, then lets git
guess.

Day headers (`## 2026-10-14`) and the `date` of a commit in `--format
json` are the **committer date** - the date `--since` and `--until` filter
on, so a commit is never labelled with a day outside the window that found
it - shown as the **local date on your machine** (`--date=format-local`),
not in the committer's timezone, so a teammate's 23:30 PDT commit lands on
the day you actually saw it. The author date (when the change was first
written) is not used: a rebase, an amend or a cherry-pick changes the
committer date and leaves the author date alone, and a commit you rebased
today is today's work.

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
(start-up errors, several repos, hang-ups, failing requests, who may ask:
Host and Origin checks, the token, the network-address refusal), bad model
answers, Slack sanitizing and escaping, the MCP server (root confinement,
invalid params, notifications, start-up check, UTF-8 wire under a cp1252
console), closed pipes (real processes, and Linux stand-ins for Windows'
errno-22 version of the error), the package metadata, a missing `git`,
the cp1252 console path, a check that no source or doc file hides an
invisible character, and a check that nothing in the package or the suite
needs more than Python 3.10. It also covers what a redirect or an error
message may reveal (the API key, the Slack webhook), which date labels a
commit, the `--format json` refusals, folders git refuses and bare
repositories, a closed standard output, and the sdist and the wheel built
from a clean copy of the tracked files (so a leftover `build/` folder
cannot get in).

Two groups of tests skip themselves, with the reason: the ones that read
`pyproject.toml` need `tomllib` (Python 3.11+), and the ones that build the
sdist and the wheel need a setuptools of 77 or later (with `packaging` 24.2
or later) already installed - they work offline and download nothing.

## Limitations

- **Dates.** Weekday names (`last monday`), `N months ago` and `N years ago`
  are worked out by git, not by `standupguy`; *Date handling* says what
  that can get wrong around a clock change. A bare weekday (`monday`) means
  today to git: the command line warns about it, `serve` and `mcp` do not.
- **Windows.** This release was developed and tested on Linux. What is
  specific to Windows (a closed pipe that is not a `BrokenPipeError`, the
  cp1252 console, UTF-8 on the MCP wire) is covered by Linux stand-ins, not
  by a run on Windows.
- **Line endings.** The repository's text files are committed with Windows
  (CRLF) line endings and there is no `.gitattributes`: keep them when you
  edit a file. On Linux and macOS `git diff` flags them as trailing
  whitespace.

## License

MIT — see `LICENSE`.

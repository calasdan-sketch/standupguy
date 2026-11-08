# Round 5: privacy, the MCP wire, the team view

Branch `expert-2`. Nothing was published, tagged, pushed or sent. Version stays
**0.3.0** (PyPI still has only 0.1.0, so 0.3.0 is unreleased).

## Honesty first

- **No independent reviewers were spawned.** One agent played each specialist
  in turn and ran real commands for each. It is one person's panel, not four.
- **Windows and macOS were not run.** Python 3.13 on Linux only for this
  round (earlier rounds also ran 3.10 and 3.12).
- **No real MCP client was tried.** The protocol-version change is checked
  against the spec as I know it, not against Claude Code, Cursor or similar.
- Competitors: the two PyPI wheels were re-read; GitHub pages were not
  (no access): git-standup and dawnlog entries stay unverified.
- `mypy` reports 20 annotation gaps (no real bugs found in them); it is not
  configured for the project and no `py.typed` was added.

## Author line (done first)

Already correct from round 4 in `pyproject.toml` and `LICENSE`. Added
`standupguy.__author__`, put it in `--version` and `standupguy doctor`, a test
that pins it everywhere (`TestAuthorLine`), and removed a real person's name
used as an example git author in README, docs strings and three test files.
No e-mail exists for the project, so none was added. Private-repo caveat does
not apply (URLs are the public GitHub ones already in the package).
Proof from a freshly built wheel and sdist:

```
$ unzip -p dist/*.whl standupguy-0.3.0.dist-info/METADATA | grep ^Author
Author: Calas Automations Inc.
$ tar xzf dist/*.tar.gz -O standupguy-0.3.0/PKG-INFO | grep ^Author
Author: Calas Automations Inc.
$ standupguy --version
standupguy 0.3.0 (by Calas Automations Inc.)
```

## Baseline (before any change)

| | |
|---|---|
| Tests | `python3 -m pytest -q`: 559 passed, 666 subtests, 178 s |
| Benchmark | grouping 96%, refs 100/100, noise 16/0, secrets leaked 0/120, false redactions 0/41, about 34,000 subjects/s |
| Lint | `ruff check --select F,E9`: clean |
| Registry | `pip index versions standupguy`: 0.1.0 only |

## Upgrade-path test (registry 0.1.0 -> this tree)

`pip download standupguy==0.1.0 --no-deps`, unzipped, run side by side with
this tree on a 3-commit repo (`--since`, `--week`, `--all`, `--format json`).
There are no state, config or cache files in either version, so nothing needs
migrating. Findings: the Markdown line shape changed on purpose in 0.2.0/0.3.0
(`(#3)` became `[PR #3]`, `1 files` became `1 file`, the `--week` caption
changed); the JSON of 0.1.0 is a strict subset of today's (new keys only:
`since`, `until`, `email`, `wip`, `coauthors`, `group`, `refs`, `agent`,
`in_flight`). Pinned by `TestUpgradeFromPublished` (the 0.1.0 key set, the
0.1.0 MCP tool call, the `AUTOSTANDUP_*` variables).

## Specialists and what each found

1. **Engineering manager running a daily standup.** On a team repo `--all`
   gives one mixed list: you cannot tell who did what. Built `--by-author`.
   Proof: `standupguy --all --by-author` on a 3-person repo (before: one
   `## Shipped`; after: one section per person, busiest first).
2. **Git-log forensics expert.** (a) A shallow clone shows its oldest commit
   as touching every file; the digest said nothing. Now a stderr note.
   Proof: `git clone --depth 1 file://... && standupguy --since 1.day.ago`.
   (b) `git replace` refs rewrite commits in `git log`; now ignored
   (`GIT_NO_REPLACE_OBJECTS`); the test fails with the line removed.
   (c) Checked and fine: `.mailmap` (old identity counts), detached HEAD,
   unborn branch, merge commits, reflog-only commits, case-insensitive
   e-mail filter (differential test below).
3. **Privacy reviewer.** Planted 13 realistic leaks in a scratch repo and
   read the default output. Leaked: a Slack webhook URL (the secret is the
   path, which public URLs keep), `Authorization: Basic ...`, `"password":
   "a pass phrase"`, `/Users/NAME` and `C:\Users\NAME`, a social security
   number, a card number, IPv6 `fd00::` and `fe80::` addresses, and
   `git.corp.example.com` came out as `{internal-url}.example.com`. Also a
   false positive: `appsettings.local.json` was shown as
   `{internal-url}.json`. All fixed. Benchmark: 35 of the 35 new planted
   cases leaked on the committed code, 0 now; ordinary strings changed by
   mistake 2 of 50 before, 0 now. Proof: `python bench/bench.py` with `redact.py` from
   `HEAD` versus now.
   Not fixable by pattern: a customer name, `db-prod-01.acme-internal.net`
   (no internal label), a passphrase not in quotes. `--redact-term` covers
   the first; the README says so.
4. **MCP server author.** (a) `mcp` ignored `STANDUPGUY_REDACT` and had no
   `--redact-term`, although its output goes to a cloud model: now
   `standupguy mcp --redact strict --redact-term X`; an agent argument
   `redact: off` is ignored (tested). (b) `initialize` always answered
   `2024-11-05`; now echoes a supported client version. (c) The tool had no
   `readOnlyHint`; added the four annotations. (d) Checked and fine: unknown
   method, bad ids, notifications, deep JSON, closed stdin, bad arguments.
5. **Locale / ops engineer.** `LC_ALL=C PYTHONUTF8=0` on a repo in
   `ré po` ended in a `UnicodeEncodeError` traceback in the worktree check.
   Fixed (`gitlog.from_git_path`), also in the agent-session reader. Proof:
   the traceback reproduced before the change; `TestOddLocale` fails without
   it. Spaces in the path: fine. Shell metacharacters in a commit subject or
   an MCP `since` argument: not run as commands (checked with a `touch`).
6. **QA / test-methodology.** Quality methods not used before, now used:
   seeded property tests (20,000 random strings through redaction: never
   raises, never grows when redacted again; the same secret in 300 random
   sentences), mutation-style sanity checks (break `_luhn`, the file
   extension list, the card prefix, the private-IP rule, and see the test
   fail), golden files for every format and both digest styles
   (`tests/golden/`), a differential test of date windows against a plain
   Python reference over 25 random (since, until, author) triples (passed
   first time), and a test that no socket is opened by the digest, the
   formats or `mcp`. Redaction is not idempotent (`password:{internal-url}`
   reads as a value the second time); documented in the test, harmless since
   it is applied once.
7. **Performance engineer.** 1 MB hostile strings (colons, digit-dash runs,
   `/Users/` x 100,000, `basic AAAA...`) through the new patterns: about 1 to
   2.5 s per MB, grows in step with size (no blow-up); the old code was about
   half that. Test `test_hostile_long_input_stays_fast` (8 s ceiling).

## Mutation checks I ran by hand

Removing `GIT_NO_REPLACE_OBJECTS` fails the replace-ref test. Setting it to
`"0"` does not (git treats any value as set), so the code uses a plain
presence. Reverting `inflight.py` fails the odd-locale test.

## Ideas, scored

User value / differentiation / effort / risk, H M L.

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | `--by-author` sections for `--all` (prior NOT-DONE #19) | H | H | M | L | DONE |
| 2 | Webhook URLs, Basic auth, quoted passphrases redacted | H | M | L | L | DONE |
| 3 | Home-folder user name hidden (`~`) | M | M | L | L | DONE |
| 4 | SSN / SIN / card (Luhn) numbers | M | M | L | M | DONE |
| 5 | Private IPv6 | L | L | L | L | DONE |
| 6 | Internal host with more labels; spare `*.local.json` files | M | L | L | L | DONE |
| 7 | MCP `--redact` / `--redact-term` / env, owner decides | H | H | L | L | DONE |
| 8 | MCP tool annotations (read-only) | M | M | L | L | DONE |
| 9 | MCP protocol-version negotiation | M | L | L | M | DONE (not tried on a real client) |
| 10 | Shallow-clone note | M | M | L | L | DONE |
| 11 | Ignore `git replace` refs | L | M | L | L | DONE |
| 12 | ASCII-locale crash fix | M | L | L | L | DONE |
| 13 | `__author__`, `--version` / `doctor` name the maker | L | L | L | L | DONE |
| 14 | Property, mutation, golden, differential, no-network tests | M | L | M | L | DONE |
| 15 | `redact` / `redact_term` as MCP tool arguments | L | L | L | H | NOT-DONE: it would let the agent loosen the owner's policy |
| 16 | `--by-author` for `serve` and `mcp` | M | M | M | L | NOT-DONE: needs a JSON shape decision |
| 17 | Per-person view in `--style ytb` | M | M | M | L | NOT-DONE: needs design |
| 18 | `--since last-standup` (remember the last run) | H | M | M | M | NOT-DONE: needs a state file, which the tool deliberately lacks |
| 19 | `--redact-file` (terms from a file; a team list) | M | L | L | L | NOT-DONE |
| 20 | Hide e-mail addresses in author names under `--by-author` | L | L | L | L | NOT-DONE: names only are shown already |
| 21 | Config file (prior #13) | M | L | M | L | NOT-DONE: `tomllib` is 3.11+ |
| 22 | `git fetch` first (prior #17) | M | L | L | M | NOT-DONE: network, changes the repo |
| 23 | Remote-only branches (prior #18) | M | L | M | M | NOT-DONE |
| 24 | Pin Actions to commit hashes (prior #22) | M | L | L | L | NOT-DONE: needs lookups |
| 25 | Trusted-publishing workflow (prior #15) | M | L | M | H | NOT-DONE: Dan's decision |
| 26 | `py.typed` + a type-check CI job (prior #10) | L | L | M | M | NOT-DONE: 20 mypy gaps first |
| 27 | Hypothesis dependency for property tests | L | L | L | L | NOT-DONE: stdlib only, seeded generator used |
| 28 | Person names redacted by default | L | L | L | H | NOT-DONE: it would break the team view |
| 29 | Detect unquoted passphrases | L | L | H | H | NOT-DONE: would invent false positives |
| 30 | Built-in holiday calendar (prior #24) | M | L | H | H | NOT-DONE: invented data |

## What could not be tested

Windows, macOS, Python 3.10/3.11/3.12/3.14 this round, a real MCP client, git
older than 2.43, GitHub pages of git-standup and dawnlog, PyPI upload,
Slack, any model endpoint. No money was spent; nothing called a paid
service.

## Numbers

| | before | after |
|---|---|---|
| Tests | 559 passed | see the final line of the commit message report |
| Benchmark, planted secrets that survive | 35 of 155 (new corpus on old code) | 0 of 155 |
| Benchmark, ordinary strings changed | 2 of 50 (`appsettings.local.json`, `next.config.local.js`) | 0 of 50 |
| Grouping / refs / noise | unchanged | unchanged |

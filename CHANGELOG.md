# Changelog

## 0.2.0

- Agent ledger: digests count the commits whose message has a `Co-Authored-By:` line naming Claude, per agent, and `--format json` has an `agent_ledger` object. It counts what the commits say, nothing more.
- `--draft --verify`: statements under Yesterday that no commit backs up are marked `[unverified: no matching commit]` (a word-overlap check; the README says what it cannot catch).
- Claude Code plugin: `.claude-plugin/marketplace.json` and `plugins/standupguy` add the skill `/standupguy:standup`, which runs `standupguy --format json`.
- `examples/standup-digest.yml`: a scheduled GitHub Actions workflow that writes the digest to the run summary and, with a `STANDUPGUY_SLACK_WEBHOOK` secret, posts it to Slack.

## 0.1.5

- `--draft`: the unencrypted-key warning no longer treats every host that starts with `127.` as this computer (`http://127.evil.com` is on the internet). Only `localhost`, a real loopback address (`127.x.x.x`, `::1`, also written `::ffff:127.0.0.1`) and `0.0.0.0` count as this computer.
- Release workflow: the tag is checked against the version of the built files (`.github/scripts/release_check.py`), and a tag whose commit is not on `main` is refused. CI builds the source archive and runs the tests from the unpacked archive.

## 0.1.4

- `--author X` no longer lists your own uncommitted files as X's "In progress" work. The section (and `in_progress` in JSON, the sprint "Carried over" list, and the `serve` / `mcp` `author` option) now appears only for you, meaning `--author` equals your git `user.email` or `user.name`, or with `--all`.
- Settings are now `STANDUPGUY_BASE_URL`, `STANDUPGUY_MODEL`, `STANDUPGUY_API_KEY` and `STANDUPGUY_SLACK_WEBHOOK`. The old `AUTOSTANDUP_*` names still work as a fallback.
- `--draft` warns on stderr when your API key would be sent unencrypted over `http://` to a host that is not this computer.
- README: the Activepieces workflow link is absolute (it was dead on PyPI), the outdated line-endings note is gone, `pip install standupguy` is the install line.
- Package metadata: authors, changelog URL, Python 3.14 classifier.

## 0.1.3

- Previous release.

# Changelog

## 0.1.4

- `--author X` no longer lists your own uncommitted files as X's "In progress" work. The section (and `in_progress` in JSON, the sprint "Carried over" list, and the `serve` / `mcp` `author` option) now appears only for you, meaning `--author` equals your git `user.email` or `user.name`, or with `--all`.
- Settings are now `STANDUPGUY_BASE_URL`, `STANDUPGUY_MODEL`, `STANDUPGUY_API_KEY` and `STANDUPGUY_SLACK_WEBHOOK`. The old `AUTOSTANDUP_*` names still work as a fallback.
- `--draft` warns on stderr when your API key would be sent unencrypted over `http://` to a host that is not this computer.
- README: the Activepieces workflow link is absolute (it was dead on PyPI), the outdated line-endings note is gone, `pip install standupguy` is the install line.
- Package metadata: authors, changelog URL, Python 3.14 classifier.

## 0.1.3

- Previous release.

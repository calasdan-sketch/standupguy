# Publish checklist for Dan (do not run until you have read it)

**What to release:** version **0.3.0**. PyPI has only 0.1.0, so this is the
first release since then and it also carries everything in the 0.2.0 section
of the CHANGELOG. It changes behaviour from 0.1.0 (the default window is now
"last workday", Markdown is redacted and escaped by default). That is fine
for a 0.x number; it is not a "patch" release. Version numbers are already
set everywhere; do not change them.

## Check first (about 5 minutes)

1. `git log --oneline -3` shows the round 4 commit on branch `expert-2`.
2. `python -m unittest discover -s tests` passes on your machine.
3. Change "unreleased" to today's date on the 0.3.0 line in `CHANGELOG.md`
   (and the 0.2.0 line, which ships with it). Commit that.
4. Make sure the GitHub Actions run is green on Windows, macOS and Linux.
   Nobody has seen the Windows run yet.
5. Read the PyPI page preview after step 6 (`twine check` does not read the
   words for you).

## Commands

```bash
python -m pip install --upgrade build twine
rm -rf dist build
python -m build
python -m twine check --strict dist/*
# try it in a throwaway folder first:
python -m venv /tmp/try && /tmp/try/bin/pip install dist/*.whl
/tmp/try/bin/standupguy doctor
# only when happy (this is the step that publishes, and it cannot be undone):
python -m twine upload dist/*
git tag v0.3.0 && git push origin v0.3.0
```

PyPI never lets you re-upload the same version number, so if something is
wrong after upload the fix is 0.3.1.

## Afterwards

- In `README.md`, change the install line to `pip install standupguy` and
  delete the paragraph that says PyPI is older than the README.
- Run `pip index versions standupguy` to confirm 0.3.0 shows.
- No cost: PyPI is free. You need a PyPI account and an API token (keep it out
  of the repo).

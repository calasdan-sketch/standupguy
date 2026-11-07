"""Round 4: what a stranger who installs the package sees in the first hour.

One version number everywhere, the install's own health check, the hint that
explains an empty digest, a date range that cannot match, and Windows-safe
path comparison.
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from test_standupguy import make_repo   # noqa: E402  (isolates git/HOME)

import standupguy                       # noqa: E402
from standupguy import cli as cli_mod   # noqa: E402
from standupguy import mcp as mcp_mod   # noqa: E402


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_mod.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _read(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return f.read()


class TestOneVersion(unittest.TestCase):
    def test_version_is_the_same_everywhere(self):
        v = standupguy.__version__
        self.assertRegex(v, r"^\d+\.\d+\.\d+$")
        self.assertEqual(cli_mod.VERSION, v)
        self.assertEqual(mcp_mod.SERVER_INFO["version"], v)
        pyproject = re.search(r'(?m)^version = "([^"]+)"', _read("pyproject.toml"))
        self.assertEqual(pyproject.group(1), v)
        # the newest heading of the changelog is this version
        first = re.search(r"(?m)^## (\d+\.\d+\.\d+)", _read("CHANGELOG.md"))
        self.assertEqual(first.group(1), v)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as cm:
            run_cli("--version")
        self.assertEqual(cm.exception.code, 0)

    def test_metadata_names_the_owner_and_the_repo(self):
        text = _read("pyproject.toml")
        self.assertIn('name = "Calas Automations Inc."', text)
        self.assertIn("https://github.com/calasdan-sketch/standupguy", text)
        self.assertIn("Calas Automations Inc.", _read("LICENSE"))


class TestDoctor(unittest.TestCase):
    def test_doctor_reports_and_exits_0(self):
        code, out, err = run_cli("doctor")
        self.assertEqual(code, 0, err)
        self.assertIn(standupguy.__version__, out)
        self.assertIn("git ", out)
        self.assertTrue(out.rstrip().endswith("ok"))

    def test_doctor_without_git_is_one_plain_message(self):
        with mock.patch.dict(os.environ, {"PATH": os.devnull}):
            code, out, err = run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("NOT FOUND", out)
        self.assertNotIn("Traceback", out + err)


class TestEmptyBecauseOfAuthor(unittest.TestCase):
    def test_hint_when_only_the_author_filter_hides_commits(self):
        root = make_repo()
        code, out, err = run_cli("--root", root, "--since", "2026-10-14",
                                 "--until", "2026-10-14",
                                 "--author", "nobody@example.com",
                                 "--no-inflight")
        self.assertEqual(code, 0)
        self.assertIn("by other authors", err)
        self.assertIn("--all", err)
        self.assertNotIn("other authors", out)

    def test_json_stays_clean(self):
        root = make_repo()
        code, out, err = run_cli("--root", root, "--since", "2026-10-14",
                                 "--until", "2026-10-14", "--format", "json",
                                 "--author", "nobody@example.com")
        self.assertEqual(code, 0)
        self.assertTrue(out.lstrip().startswith("{"))
        import json
        json.loads(out)

    def test_scan_stays_quiet_and_several_roots_are_named(self):
        root = make_repo()
        parent = tempfile.mkdtemp()
        shutil.move(make_repo(), os.path.join(parent, "one"))
        code, out, err = run_cli("--scan", parent,
                                 "--scan-depth", "1",
                                 "--since", "2026-10-14",
                                 "--until", "2026-10-14",
                                 "--author", "nobody@example.com")
        self.assertNotIn("other authors", err)
        other = make_repo()
        code, out, err = run_cli("--root", root, "--root", other,
                                 "--since", "2026-10-14",
                                 "--until", "2026-10-14",
                                 "--author", "nobody@example.com")
        self.assertIn(f" in {os.path.basename(root)} in this window", err)

    def test_no_hint_when_nothing_at_all(self):
        root = make_repo()
        code, out, err = run_cli("--root", root, "--since", "2020-01-01",
                                 "--until", "2020-01-02",
                                 "--author", "nobody@example.com")
        self.assertEqual(code, 0)
        self.assertNotIn("other authors", err)

    def test_no_hint_when_commits_were_found(self):
        root = make_repo()
        code, out, err = run_cli("--root", root, "--since", "2026-10-14",
                                 "--until", "2026-10-14",
                                 "--author", "dan@example.com")
        self.assertNotIn("other authors", err)


class TestBackwardsRange(unittest.TestCase):
    def test_since_after_until_is_an_error(self):
        root = make_repo()
        code, out, err = run_cli("--root", root, "--since", "2026-10-15",
                                 "--until", "2026-10-14")
        self.assertEqual(code, 2)
        self.assertIn("swap them", err)
        self.assertEqual(out, "")

    def test_same_day_and_ordered_ranges_are_fine(self):
        root = make_repo()
        for since, until in (("2026-10-14", "2026-10-14"),
                             ("2026-10-13", "2026-10-14")):
            code, _, err = run_cli("--root", root, "--since", since,
                                   "--until", until)
            self.assertEqual(code, 0, err)


class TestPathCompare(unittest.TestCase):
    def test_same_path_uses_normcase(self):
        with mock.patch("os.path.normcase", side_effect=str.lower):
            self.assertEqual(cli_mod._same_path("/TMP/Some/Where"),
                             cli_mod._same_path("/tmp/some/where"))


if __name__ == "__main__":
    unittest.main()

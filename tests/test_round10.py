"""Round 10 regressions.

R10-1 The release workflow could not be trusted (see test_release_gate.py).
R10-2 insecure_key_warning treated any host starting with "127." as this
      computer, so `http://127.evil.com` sent the key without a word.
"""

import contextlib
import io
import unittest
from unittest import mock

from test_standupguy import cli_mod, draft_mod  # noqa: F401

warn = draft_mod.insecure_key_warning
KEY = "sk-test-0123456789"


class TestKeyWarningHostCheck(unittest.TestCase):
    def test_a_name_that_starts_with_127_is_not_this_computer(self):
        for url in ("http://127.evil.com/v1", "http://127.0.0.1.evil.com/v1",
                    "http://127.example.org:8080/v1", "http://127.a/v1"):
            self.assertIsNotNone(warn(url, KEY), url)

    def test_a_name_that_starts_with_localhost_is_not_this_computer(self):
        for url in ("http://localhost.evil.com/v1", "http://localhostx/v1",
                    "http://notlocalhost/v1"):
            self.assertIsNotNone(warn(url, KEY), url)

    def test_this_computer_stays_quiet(self):
        for url in ("http://localhost:11434/v1", "http://LOCALHOST/v1",
                    "http://localhost./v1", "http://127.0.0.1:1/v1",
                    "http://127.0.0.2/v1", "http://127.255.255.254/v1",
                    "http://[::1]:11434/v1", "http://0.0.0.0:11434/v1",
                    "http://[::ffff:127.0.0.1]/v1"):
            self.assertIsNone(warn(url, KEY), url)

    def test_other_addresses_warn(self):
        for url in ("http://10.0.0.5/v1", "http://192.168.1.2:11434/v1",
                    "http://8.8.8.8/v1", "http://[2001:db8::1]/v1",
                    "http://[::ffff:10.0.0.1]/v1", "http://128.0.0.1/v1",
                    "http://1.127.0.0.1/v1"):
            self.assertIsNotNone(warn(url, KEY), url)

    def test_odd_spellings_of_loopback_warn_rather_than_pass(self):
        # "127.1" is 127.0.0.1 to some resolvers; a warning that was not
        # needed is better than silence that was not deserved.
        for url in ("http://127.1/v1", "http://2130706433/v1"):
            self.assertIsNotNone(warn(url, KEY), url)

    def test_https_never_warns(self):
        self.assertIsNone(warn("https://127.evil.com/v1", KEY))
        self.assertIsNone(warn("https://api.example.com/v1", KEY))

    def test_no_key_no_warning(self):
        self.assertIsNone(warn("http://127.evil.com/v1", ""))
        self.assertIsNone(warn("http://127.evil.com/v1", "ollama"))

    def test_the_warning_names_the_host_not_the_key(self):
        text = warn("http://user:pw@127.evil.com:99/v1?k=1", KEY)
        self.assertIn("127.evil.com", text)
        for secret in (KEY, "pw", "user", "k=1"):
            self.assertNotIn(secret, text)

    def test_a_broken_url_does_not_raise(self):
        self.assertIsNone(warn("http://[::1/v1", KEY))

    def test_is_this_computer_directly(self):
        f = draft_mod._is_this_computer
        self.assertTrue(f("localhost"))
        self.assertTrue(f("127.0.0.1"))
        self.assertTrue(f("::1"))
        self.assertTrue(f("::1%lo"))
        self.assertFalse(f("127.evil.com"))
        self.assertFalse(f(""))
        self.assertFalse(f("example.com"))

    def test_draft_prints_the_warning_before_sending(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(draft_mod.urllib.request.OpenerDirector,
                                  "open", side_effect=OSError("stop here")):
            with self.assertRaises(draft_mod.DraftError):
                draft_mod.draft("# digest", base_url="http://127.evil.com/v1",
                                api_key=KEY, model="m")
        self.assertIn("unencrypted", err.getvalue())
        self.assertNotIn(KEY, err.getvalue())


if __name__ == "__main__":
    unittest.main()

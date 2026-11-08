"""Redaction: what must not reach a pasted public channel.

A standup is pasted into Slack, Teams, a wiki, a model prompt. Commit
messages and file names hold what their authors typed in a hurry: a token
in a test, a link to an internal wiki, a customer name, a private IP.

Profiles
  off       nothing is changed.
  standard  (default for text) secrets and credentials; the query string,
            login and fragment of every URL; URLs on internal hosts
            (`*.internal`, `*.corp`, `*.local`, `*.lan`, `localhost`, single-
            word hosts, private and link-local IPs); e-mail addresses;
            private IP addresses.
  strict    standard, plus every URL, whatever its host, and the repository
            names (`project-1`, `project-2` ... in the order they appear).

`--redact-term WORD` (repeatable) adds literal words, such as a customer or
an unannounced product, that become `{redacted}` in any profile but `off`.

Pattern matching finds shapes, not intent: it will miss a secret with no
recognisable shape, and say so (see README, Limits). Nothing here makes a
network call.
"""

from __future__ import annotations

import math
import re

PROFILES = ("off", "standard", "strict")
DEFAULT_PROFILE = "standard"

# Token shapes. Each alternative starts at a boundary so that a SHA, a path or
# a word is not eaten.
_SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----(?:.{0,6000}?-----END [A-Z ]{0,20}PRIVATE KEY-----)?", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxox[abprsoe]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
    # name = value / name: value, where the name says it is a secret and
    # the value looks like one (6+ characters, with a digit or a symbol)
    re.compile(
        r"(?i)(\b(?:pass(?:word|wd)?|secret|token|api[_\-]?key|apikey|"
        r"access[_\-]?key|private[_\-]?key|auth(?:orization)?|credentials?)"
        r"[\"']?\s*[:=]\s*[\"']?)(?=[^\s\"',;]*[0-9_\-/+=!@#$%^&*])[^\s\"',;]{6,}"),
]
_ASSIGN_RE = _SECRET_RES[-1]

# A webhook URL is a secret whole: whoever has it can post as you, and on
# these hosts the secret is the PATH, which the public-URL rule keeps.
_WEBHOOK_RES = [re.compile(p, re.I) for p in (
    r"https?://hooks\.slack\.com/(?:services|workflows|triggers)/[^\s<>\"'`)\]]+",
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/"
    r"\d+/[A-Za-z0-9_\-]+",
    r"https?://[A-Za-z0-9.\-]*webhook\.office\.com/[^\s<>\"'`)\]]+",
    r"https?://outlook\.office(?:365)?\.com/webhook/[^\s<>\"'`)\]]+",
    r"https?://hooks\.zapier\.com/hooks/[^\s<>\"'`)\]]+",
    r"https?://api\.telegram\.org/bot\d+:[A-Za-z0-9_\-]+",
)]
# `Authorization: Basic dXNlcjpwYXNz` (a word like "basic infrastructure"
# has no digit, `=` or `+` in it)
_BASIC_RE = re.compile(
    r"(?i)\bbasic\s+(?=[A-Za-z0-9+/]*[0-9=+/])[A-Za-z0-9+/]{16,}={0,2}")
# "password": "a pass phrase"  /  secret = 'two words': the quotes say it
# is a value, so the whole quoted text goes, digit or not
_QUOTED_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:pass(?:word|wd|phrase)?|secret|token|api[_\-]?key|apikey|"
    r"access[_\-]?key|private[_\-]?key|credentials?)[\"']\s*[:=]\s*|"
    r"\b(?:pass(?:word|wd|phrase)?|secret|api[_\-]?key)\s*=\s*)"
    r"(\"[^\"\n]{4,200}\"|'[^'\n]{4,200}')")
# the user name inside a home folder is a person's name (`/Users/`, as
# macOS spells it, exactly: `/users/123` is a REST route)
_HOME_RE = re.compile(
    r"(/Users/|(?i:\b[A-Za-z]:[\\/]+Users[\\/]+))([^/\\\s:\"'`)\]<>]+)")
# /home/NAME looks the same as a web route (/home/dashboard): strict only
_HOME_LINUX_RE = re.compile(r"(/home/)([^/\\\s:\"'`)\]<>]+)")
_CARD_RE = re.compile(r"(?<![\w.\-])(?:\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}"
                      r"|\d{15,16})(?![\w\-])")
_SSN_RE = re.compile(r"(?<![\d\-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)"
                     r"\d{4}(?![\d\-])")
_SIN_RE = re.compile(r"(?<![\d\-])\d{3}[ -]\d{3}[ -]\d{3}(?![\d\-])")
# private IPv6: unique-local fc00::/7 and link-local fe80::/10
_IP6_RE = re.compile(
    r"(?i)(?<![\w:])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):"
    r"(?:[0-9a-f]{0,4}:){1,6}[0-9a-f]{0,4}(?![\w:])")
_CARD_PREFIX_RE = re.compile(r"4|5[1-5]|2[2-7]|3[47]|6011|65")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

_URL_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.\-]{1,15})://([^\s<>\"'`)\]]{1,2000})")
# every repetition is bounded and starts at a boundary: a 1 MB commit
# subject of one repeated character must stay linear
_BARE_INTERNAL_RE = re.compile(
    r"(?<![\w.\-])(?:[A-Za-z0-9\-]{1,63}\.){1,8}"
    r"(?:internal|corp|local|lan|intranet|home|private)"
    r"(?:\.[A-Za-z0-9\-]{1,63}){0,4}"
    r"(?::\d{1,5})?(?:/[^\s<>\"'`)\]]{0,300})?(?![\w])", re.I)

# `appsettings.local.json`, `next.config.local.js`: a file, not a host
_FILE_EXTS = frozenset((
    "json js ts tsx jsx mjs cjs py rb php yml yaml toml ini cfg conf env md "
    "txt sh bat ps1 xml html css scss lock log sql csv properties gradle "
    "java kt go rs example sample dist bak template tpl").split())
_EMAIL_RE = re.compile(
    r"(?<![\w.%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}"
    r"(?:\.[A-Za-z0-9\-]{1,63}){1,8}")
_IP_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/_\-])")

_INTERNAL_LABELS = frozenset(("internal", "corp", "intranet", "local", "lan",
                              "private", "localdomain", "staging", "stage"))
_INTERNAL_SUFFIXES = (".internal", ".corp", ".local", ".lan", ".intranet",
                      ".home", ".private", ".localdomain")


def _entropy(s: str) -> float:
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _private_ip(m) -> bool:
    a, b, c, d = (int(x) for x in m.groups())
    if max(a, b, c, d) > 255:
        return False
    return (a == 10 or a == 127 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or (a == 169 and b == 254))


def _internal_host(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in ("localhost", "::1") or host.endswith(_INTERNAL_SUFFIXES):
        return True
    if any(label in _INTERNAL_LABELS for label in host.split(".")):
        return True
    if "." not in host and ":" not in host:     # one-word host: an intranet name
        return True
    m = _IP_RE.fullmatch(host)
    return bool(m and _private_ip(m))


def _host_of(rest: str) -> str:
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    authority = authority.rsplit("@", 1)[-1]
    if authority.startswith("["):
        return authority.split("]")[0] + "]"
    return authority.split(":")[0]


class Redactor:
    """A redaction profile applied to one piece of text at a time."""

    def __init__(self, profile: str = DEFAULT_PROFILE, terms=()):
        if profile not in PROFILES:
            raise ValueError(f"redaction profile must be one of "
                             f"{', '.join(PROFILES)}")
        self.profile = profile
        words = [t.strip() for t in terms if t and t.strip()]
        self._terms = (re.compile("|".join(
            re.escape(w) for w in sorted(words, key=len, reverse=True)),
            re.I) if words else None)
        self._repos: dict = {}
        self.count = 0

    @property
    def active(self) -> bool:
        return self.profile != "off"

    def _hit(self, label: str) -> str:
        self.count += 1
        return label

    def repo(self, name: str) -> str:
        """A repository name as the profile shows it."""
        if self.profile == "strict":
            return self._repos.setdefault(name, f"project-{len(self._repos) + 1}")
        return self.text(name)

    def person(self, name: str, number: int) -> str:
        """A person's name as the profile shows it: strict makes them
        `person-1`, `person-2` ... (by rank within one repository's digest,
        busiest first: the label says nothing about who it is); the others
        show the name."""
        if self.profile == "strict":
            return f"person-{number}"
        return self.text(name)

    def text(self, s: str) -> str:
        if not self.active or not s:
            return s
        if self._terms:
            s = self._terms.sub(lambda m: self._hit("{redacted}"), s)
        for rx in _WEBHOOK_RES:
            s = rx.sub(lambda m: self._hit("{secret}"), s)
        for rx in _SECRET_RES[:-1]:
            s = rx.sub(lambda m: self._hit("{secret}"), s)
        s = _BASIC_RE.sub(lambda m: self._hit("{secret}"), s)
        s = _QUOTED_ASSIGN_RE.sub(
            lambda m: m.group(1) + self._hit('"{secret}"'), s)
        # URLs get their own rules; the other patterns look at the text
        # between them only, so a public link is not mangled twice
        out, pos = [], 0
        for m in _URL_RE.finditer(s):
            out.append(self._plain(s[pos:m.start()]))
            out.append(self._url(m))
            pos = m.end()
        out.append(self._plain(s[pos:]))
        return "".join(out)

    def _plain(self, s: str) -> str:
        s = _ASSIGN_RE.sub(lambda m: m.group(1) + self._hit("{secret}"), s)
        s = _HOME_RE.sub(self._home, s)
        if self.profile == "strict":
            s = _HOME_LINUX_RE.sub(self._home, s)
        s = _BARE_INTERNAL_RE.sub(self._bare_internal, s)
        s = _SSN_RE.sub(lambda m: self._hit("{id-number}"), s)
        s = _SIN_RE.sub(self._sin, s)
        s = _CARD_RE.sub(self._card, s)
        s = _IP6_RE.sub(lambda m: self._hit("{ip}"), s)
        s = _EMAIL_RE.sub(lambda m: self._hit("{email}"), s)
        s = _IP_RE.sub(lambda m: self._hit("{ip}") if _private_ip(m) else m.group(0), s)
        s = _LONG_TOKEN_RE.sub(self._long, s)
        return s

    def _home(self, m) -> str:
        """/Users/dan/x becomes ~/x: the folder keeps its shape, the
        person's name goes (not a system name such as Shared)."""
        if m.group(2).lower() in ("shared", "public", "default", "all"):
            return m.group(0)
        self.count += 1
        return "~"

    def _bare_internal(self, m) -> str:
        text = m.group(0)
        parts = text.lower().split(".")
        if len(parts) >= 3 and parts[-1] in _FILE_EXTS \
                and parts[-2] in _INTERNAL_LABELS \
                and ":" not in text and "/" not in text:
            return text                     # appsettings.local.json
        return self._hit("{internal-url}")

    def _card(self, m) -> str:
        text = m.group(0)
        digits = re.sub(r"\D", "", text)
        if 13 <= len(digits) <= 19 and _luhn(digits) and (
                digits != text or _CARD_PREFIX_RE.match(digits)):
            return self._hit("{card}")
        return text

    def _sin(self, m) -> str:
        text = m.group(0)
        return self._hit("{id-number}") if _luhn(re.sub(r"\D", "", text)) \
            else text

    def _url(self, m) -> str:
        scheme, rest = m.group(1), m.group(2)
        if self.profile == "strict":
            return self._hit("{link}")
        host = _host_of(rest)
        if _internal_host(host):
            return self._hit("{internal-url}")
        # a public URL stays, without login, query string and fragment
        authority, sep, path = rest.partition("/")
        cleaned = authority.rsplit("@", 1)[-1] + (sep + path if sep else "")
        cleaned = re.split(r"[?#]", cleaned, maxsplit=1)[0]
        if cleaned != rest:
            self.count += 1
        return f"{scheme}://{cleaned}"

    def _long(self, m) -> str:
        tok = m.group(0)
        if re.fullmatch(r"[0-9a-fA-F]+", tok):      # a SHA or a hash: keep
            return tok
        if "/" in tok and not re.search(r"[0-9]", tok):  # a path or words
            return tok
        has_digit = any(ch.isdigit() for ch in tok)
        has_alpha = any(ch.isalpha() for ch in tok)
        if has_digit and has_alpha and _entropy(tok) >= 3.5:
            return self._hit("{secret}")
        return tok


def make(profile: str = DEFAULT_PROFILE, terms=()) -> Redactor:
    return Redactor(profile, terms)

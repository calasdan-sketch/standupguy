"""Ticket keys and pull-request numbers found in a commit message.

`ABC-123` (Jira, Linear, YouTrack style), `#123` issues, `GH-123`, the
pull request a squash or merge commit came from (`(#412)`, `Merge pull
request #55`, `Merged in x (pull request #7)`, GitLab's `See merge request
g/p!9`), and what a commit says it closes (`closes #77`).

Precision over recall: a lowercase `proj-10` is not a key (it is also
`utf-8`, `sha-256`, `x-1`), and a stop list removes the upper-case look-alikes
(`UTF-8`, `SHA-256`, `ISO-8601`, `CVE-2024-1234`, `GPT-4`). The benchmark in
`bench/` measures precision and recall on a labelled corpus.
"""

from __future__ import annotations

import dataclasses
import re

# Upper-case project key, a dash, a number. Not glued to other letters,
# digits, "_" or "." in front, not followed by a letter/digit/"_" or by
# "-digit" (CVE-2024-1234 is one identifier, not the key CVE-2024).
_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Z][A-Z0-9]{1,9})-([1-9][0-9]{0,5})"
    r"(?![A-Za-z0-9_]|-[0-9])")

#: Upper-case words that look like a key but are standards, algorithms
#: or file formats.
NOT_A_KEY = frozenset((
    "UTF", "SHA", "ISO", "RFC", "CVE", "CWE", "HTTP", "HTTPS", "TLS", "SSL",
    "AES", "RSA", "PEP", "WCAG", "ECMA", "IPV", "UTC", "GMT", "MD", "BIP",
    "HTML", "CSS", "SQL", "API", "JSON", "UNIX", "POSIX", "ES", "OAUTH",
    "OIDC", "BCP", "IEEE", "ASCII", "GPT", "LLM", "USB", "PDF", "PNG",
    "MP", "GB", "MB", "KB", "TB", "CRC", "RC", "TCP", "UDP", "DNS", "IPV6",
    "ARM", "AMD", "X", "V", "H", "COVID", "SARS", "WPA", "BLAKE", "SHAKE",
    "UUID", "ULID", "LF", "CRLF", "ANSI", "UTF16", "UTF8", "PY", "NODE",
    "CP", "ISO8601", "GH",
))

_ISSUE_RE = re.compile(r"(?<![\w&/#])#([1-9][0-9]{0,6})(?![\w])")
_GH_RE = re.compile(r"(?<![A-Za-z0-9_])[Gg][Hh]-([1-9][0-9]{0,6})(?![A-Za-z0-9_])")
_SQUASH_PR_RE = re.compile(r"\(#([1-9][0-9]{0,6})\)\s*$")
_MERGE_PR_RE = re.compile(
    r"merge pull request #([1-9][0-9]{0,6})"
    r"|\(pull request #([1-9][0-9]{0,6})\)"
    r"|see merge request [^\s!]*![ ]?([1-9][0-9]{0,6})"
    r"|merge request !([1-9][0-9]{0,6})",
    re.IGNORECASE)
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[:\s]+"
    r"((?:#[1-9][0-9]{0,6}(?:[\s,]+(?:and\s+)?)?)+)", re.IGNORECASE)

#: At most this many of each kind are kept per commit.
MAX_REFS = 10


@dataclasses.dataclass(frozen=True)
class Refs:
    tickets: tuple = ()     # ("ABC-123", ...) in order of first appearance
    issues: tuple = ()      # (77, ...) numbers of #77 / GH-77
    prs: tuple = ()         # pull/merge request numbers this commit came from
    closes: tuple = ()      # issue numbers the message says it closes

    def __bool__(self) -> bool:
        return bool(self.tickets or self.issues or self.prs)


def _uniq(items) -> tuple:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out[:MAX_REFS])


def extract(subject: str, body: str = "") -> Refs:
    """The references in a commit's subject (and body)."""
    subject = subject or ""
    text = subject + "\n" + (body or "")
    prs = []
    m = _SQUASH_PR_RE.search(subject)
    if m:
        prs.append(int(m.group(1)))
    for m in _MERGE_PR_RE.finditer(text):
        prs.append(int(next(g for g in m.groups() if g)))
    tickets = [f"{k}-{n}" for k, n in _KEY_RE.findall(text)
               if k not in NOT_A_KEY and sum(ch.isalpha() for ch in k) >= 2]
    # "#412" that is the squash PR number is a pull request, not an issue
    issues = [int(n) for n in _ISSUE_RE.findall(text)]
    issues += [int(n) for n in _GH_RE.findall(text)]
    issues = [n for n in issues if n not in prs]
    closes = []
    for m in _CLOSES_RE.finditer(text):
        closes += [int(n) for n in re.findall(r"#([0-9]+)", m.group(1))]
    return Refs(_uniq(tickets), _uniq(issues), _uniq(prs), _uniq(closes))


def _in_text(text: str, token: str) -> bool:
    """Is `token` (`ABC-12`, `#77`) already written in `text` as a whole
    word? Then a tag for it would only repeat what the line says."""
    if not text or token not in text:
        return False
    i = text.find(token)
    while i != -1:
        before = text[i - 1] if i else ""
        after = text[i + len(token)] if i + len(token) < len(text) else ""
        if not (before.isalnum() or before == "_") \
                and not (after.isalnum() or after == "_" or
                         (after == "-" and token[-1].isdigit())):
            return True
        i = text.find(token, i + 1)
    return False


def tags(refs: Refs, text: str = "") -> str:
    """Refs as the bracketed tags a digest line shows: `[ABC-123] [#77]
    [PR #412]`. Empty string when there are none. A ticket or issue that
    the line `text` already names is not tagged a second time."""
    parts = [f"[{t}]" for t in refs.tickets if not _in_text(text, t)]
    parts += [f"[#{n}]" for n in refs.issues if not _in_text(text, f"#{n}")]
    parts += [f"[PR #{n}]" for n in refs.prs]
    return " ".join(parts)

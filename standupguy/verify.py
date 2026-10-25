"""Verify mode: flag the sentences of an LLM draft that no commit backs up.

`--draft` asks a model to write Yesterday / Today / Blockers from the digest.
A model can state something that was never done. `--verify` checks the
claims in the **Yesterday** part of the draft (what was accomplished) against
the commits of the digest and marks each statement that has nothing to do
with any of them:

    - Rewrote the billing engine in Rust.  [unverified: no matching commit]

It is a word-overlap check, nothing smarter, and deterministic: a statement
is backed up when it names a commit (a 7+ character sha that starts one, or
a pull request number such as #42) or shares at least one meaningful word
with a commit - its subject, or the name of a file or folder it touched.
So it catches an invented topic; it does NOT catch a wrong detail about a
real topic ("fixed the login bug" passes if a commit mentions login), and a
statement worded with none of the commit's words is flagged even if it is
true. Today and Blockers are plans, not claims about commits, and are not
checked. Nothing is sent anywhere.
"""

from __future__ import annotations

import re

MARK = "[unverified: no matching commit]"

#: Words that say what was done to something rather than what: they match
#: every commit, so they prove nothing.
_GENERIC = frozenset("""
a about above after again all also an and any are around as at back be been
before being but by can code could did do does done each fix fixed fixes
fixing for from get got had has have help helped in into is it its just made
make makes making more most much new not now of on one only onto or other our
out over per release released same set some such than that the their them
then there these they this those through to too up update updated updates
updating use used uses using very was we were what when which while will
with without work worked working would yet you add added adds adding change
changed changes changing implement implemented implements improve improved
improves improving merge merged merges refactor refactored remove removed
removes rename renamed renames start started continued continue finish
finished complete completed ship shipped yesterday today day week sprint
commit commits pr prs pull request requests branch main
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9_]{2,}")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_PR = re.compile(r"(?:#|\bpr\s*#?|\bpull request\s*#?)(\d+)", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(?:#+\s*)?\**(yesterday|today|blockers?)\**\s*:?\**\s*$",
                      re.IGNORECASE)
_NOTHING = re.compile(r"\b(no commits?|nothing|none|thin|no activity|"
                      r"no work)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def stem(word: str) -> str:
    """A crude, stable stem: lower case, six characters (so authenticate,
    authentication and authenticated meet)."""
    return word.lower().strip("_")[:6]


def words(text: str) -> set[str]:
    """The meaningful stems in `text`."""
    return {stem(w) for w in _WORD.findall(text.lower())
            if w not in _GENERIC and not w.isdigit()}


def _commit_words(commit: dict) -> set[str]:
    parts = [commit.get("subject") or ""]
    for path in commit.get("files") or []:
        parts.append(path.replace("/", " ").replace("\\", " ")
                     .replace(".", " ").replace("-", " ")
                     .replace("_", " "))
    out: set[str] = set()
    for part in parts:
        out |= words(part.replace("-", " ").replace("_", " "))
    return out


def supported(sentence: str, commits: list[dict]) -> bool:
    """Is this statement backed up by at least one commit?

    A statement that names a commit or a pull request is judged by that
    reference alone: a sha or PR number no commit has is a claim about
    something that is not there, whatever else the sentence says."""
    low = sentence.lower()
    shas = [m.group(0) for m in _SHA.finditer(low)
            if any(ch.isdigit() for ch in m.group(0))]
    prs = {int(m.group(1)) for m in _PR.finditer(sentence)}
    if shas or prs:
        if any((c.get("sha") or "").lower().startswith(x)
               for x in shas for c in commits):
            return True
        return bool(prs) and any(c.get("pr") in prs for c in commits)
    mine = words(sentence)
    if not mine:
        return True            # nothing to check ("Done.")
    return any(mine & _commit_words(c) for c in commits)


def _yesterday_range(lines: list[str]) -> range:
    """The line numbers the claims are in: from the Yesterday heading to the
    next heading, or every line when the draft has no such heading."""
    start = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(1).lower() == "yesterday":
            start = i + 1
            break
    if start is None:
        return range(len(lines))
    end = len(lines)
    for i in range(start, len(lines)):
        if _HEADING.match(lines[i]):
            end = i
            break
    return range(start, end)


def check(draft: str, commits: list[dict]) -> list[tuple[int, str]]:
    """[(line number, statement)] for the statements in the Yesterday part
    of `draft` that no commit backs up. Lines are numbered from 0."""
    lines = draft.split("\n")
    flagged: list[tuple[int, str]] = []
    for n in _yesterday_range(lines):
        text = lines[n].strip().lstrip("-*+ ").strip()
        if not text or _HEADING.match(lines[n]):
            continue
        for sentence in _SENTENCE_END.split(text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if not commits:
                ok = bool(_NOTHING.search(sentence))
            else:
                ok = supported(sentence, commits)
            if not ok:
                flagged.append((n, sentence))
    return flagged


def annotate(draft: str, flagged: list[tuple[int, str]]) -> str:
    """The draft with MARK added to the end of every line that holds a
    flagged statement (once per line)."""
    marked = {n for n, _ in flagged}
    lines = [f"{line}  {MARK}" if i in marked else line
             for i, line in enumerate(draft.split("\n"))]
    return "\n".join(lines)


def verify(draft: str, commits: list[dict]) -> tuple[str, list[tuple[int, str]]]:
    """(the draft with unbacked statements marked, the flagged statements)."""
    flagged = check(draft, commits)
    return annotate(draft, flagged), flagged

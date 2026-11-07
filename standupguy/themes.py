"""Group commits by what they did, and fold away what is only noise.

`digest.classify` reads a conventional-commit prefix and nothing else, so
"Add login page" and "Fix crash on logout" - how most people really write -
all land in "Other". `classify_smart` also reads the leading verb, the
branch name of a merge commit, the files touched and well-known words. It is
deterministic (no model) and measured in `bench/`.

`fold` removes what a teammate does not need to read: `fixup!` / `squash!`
commits that belong to another commit in the window, bare `wip` / `tmp` /
`save` saves, "Merge branch 'main' into ..." syncs, a revert together with
the commit it undoes, and duplicates (a cherry-pick or a rebase that left
the same subject twice). What was folded is counted and shown, never hidden.
"""

from __future__ import annotations

import dataclasses
import re

from . import refs as refs_mod

GROUPS = ("feat", "fix", "refactor", "docs", "test", "chore", "other")

_CONV_RE = re.compile(r"^(\w+)(?:\(([^)]*)\))?(!)?:\s*(.+)$", re.S)
_CONV_ALIASES = {
    "feature": "feat", "feat": "feat", "add": "feat",
    "fix": "fix", "bugfix": "fix", "hotfix": "fix", "bug": "fix",
    "refactor": "refactor", "perf": "refactor", "style": "chore",
    "docs": "docs", "doc": "docs", "test": "test", "tests": "test",
    "chore": "chore", "build": "chore", "ci": "chore", "deps": "chore",
    "release": "chore", "revert": "chore",
}

# first word of the subject (lower case) -> group
_VERBS = {}
for _group, _words in {
    "feat": "add adds added adding implement implements implemented introduce "
            "introduces create creates created support supports enable enables "
            "allow allows new build builds expose provide provides integrate "
            "wire hook scaffold feature",
    "fix": "fix fixes fixed fixing bugfix hotfix resolve resolves resolved "
           "correct corrects corrected repair patch handle handles prevent "
           "prevents avoid avoids stop stops guard workaround",
    "refactor": "refactor refactors refactored restructure rework reworks "
                "simplify simplifies extract extracts rename renames renamed "
                "move moves moved split merge-logic reorganize reorganise "
                "cleanup optimize optimise improve improves speed deduplicate "
                "dedupe inline decouple",
    "docs": "docs doc document documents documented readme changelog comment "
            "comments typo typos",
    "test": "test tests testing spec specs cover covers coverage mock mocks",
    "chore": "bump bumps upgrade upgrades upgraded downgrade pin pins lint "
             "format reformat prettier release version tag publish deps "
             "dependencies dependency ci lockfile gitignore config configure "
             "chore revert reverts merge remove removes removed delete deletes drop drops "
             "update-deps",
}.items():
    for _w in _words.split():
        _VERBS.setdefault(_w, _group)

_PHRASES = (
    (re.compile(r"\b(?:clean ?up|tidy|refactor\w*)\b", re.I), "refactor"),
    (re.compile(r"\b(?:fix(?:es|ed)?|bug|crash|regression|hotfix|broken)\b",
                re.I), "fix"),
    (re.compile(r"\b(?:readme|changelog|docs?|documentation|typo)\b", re.I),
     "docs"),
    (re.compile(r"\b(?:tests?|specs?|coverage)\b", re.I), "test"),
    (re.compile(r"\b(?:bump|dependabot|lockfile|dependencies)\b", re.I),
     "chore"),
)

_BRANCH_GROUPS = {
    "feature": "feat", "feat": "feat", "fix": "fix", "bugfix": "fix",
    "hotfix": "fix", "docs": "docs", "doc": "docs", "chore": "chore",
    "refactor": "refactor", "test": "test", "tests": "test", "ci": "chore",
    "release": "chore", "deps": "chore",
}

_MERGE_PR_SUBJECT_RE = re.compile(
    r"^merge pull request #\d+ from (\S+)", re.I)
_MERGE_BRANCH_RE = re.compile(
    r"^merge (?:remote-tracking )?branch '?[^ ']+'?(?: of \S+)?"
    r"(?: into '?[^ ']+'?)?\s*$", re.I)
_MERGE_REQUEST_RE = re.compile(r"^merge branch '([^']+)' into '[^']+'", re.I)

_DOC_PATH_RE = re.compile(r"(?:^|/)(?:docs?|documentation)/|\.(?:md|rst|adoc)$",
                          re.I)
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__|e2e)/|(?:^|/)test_[^/]*$"
    r"|[._-](?:test|spec)\.[a-z0-9]+$", re.I)
_CHORE_PATH_RE = re.compile(
    r"(?:^|/)(?:\.github|\.circleci|\.gitlab-ci\.yml|dockerfile|makefile)"
    r"|(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
    r"cargo\.lock|go\.sum|gemfile\.lock|requirements[^/]*\.txt|"
    r"pyproject\.toml|\.gitignore|\.editorconfig)$", re.I)

_JUNK_RE = re.compile(
    r"^(?:wip|tmp|temp|save|saved|checkpoint|misc|stuff|things|asdf+|x+|ok|"
    r"more|changes?|update|updates|commit|testing|test|fix|fixes|minor|"
    r"small ?fix|progress|work in progress|\.+|-+|\?+|done|final|again|"
    r"oops|ups|typo|lint|formatting|format|cleanup|clean up|merge|sync)"
    r"[\s.!]*$", re.I)
_FIXUP_RE = re.compile(r"^(fixup|squash|amend)!\s*(.*)$", re.I | re.S)
_REVERT_RE = re.compile(r'^revert\s+"(.+)"\s*$', re.I)
_REVIEW_RE = re.compile(
    r"^(?:address(?:ed|ing)?|apply|applied|incorporate)\s+"
    r"(?:pr |code )?(?:review|feedback|comments?)", re.I)

_TICKET_PREFIX_RE = re.compile(
    r"^(?:\[\s*([A-Z][A-Z0-9]+-\d+)\s*\]|([A-Z][A-Z0-9]+-\d+))\s*[:\-–—]?\s*")
_TRAILING_PR_RE = re.compile(r"\s*\(#\d+\)\s*$")
_BOT_RE = re.compile(r"dependabot|renovate", re.I)
_CHORE_WORD_RE = re.compile(r"\.gitignore|\.editorconfig|lockfile|dependabot|renovate",
                            re.I)
_WIP_TAG_RE = re.compile(r"^\s*(?:\[wip\]|\bwip\b)[:\s\-]+", re.I)
_GITMOJI_RE = re.compile(r"^(?::[a-z0-9_+\-]+:|[\U0001F300-\U0001FAFF☀-➿])\s*")

_GITMOJI = {
    ":sparkles:": "feat", ":bug:": "fix", ":ambulance:": "fix",
    ":memo:": "docs", ":recycle:": "refactor", ":white_check_mark:": "test",
    ":wrench:": "chore", ":arrow_up:": "chore", ":fire:": "chore",
}


def _conv(subject: str):
    """(group, scope, text) for a conventional-commit subject, else None."""
    m = _CONV_RE.match(subject.strip())
    if not m:
        return None
    word = m.group(1).lower()
    group = _CONV_ALIASES.get(word)
    if group is None:
        return None
    return group, (m.group(2) or "").strip(), m.group(4).strip()


def clean_subject(subject: str) -> str:
    """The subject without its conventional prefix, a leading ticket key
    (`ABC-123: `, `[ABC-123] `), a trailing `(#412)` and a gitmoji, with
    the first letter lower-cased only if it was a prefix that went away."""
    s = (subject or "").strip()
    m = _GITMOJI_RE.match(s)
    if m and len(s) > m.end():
        s = s[m.end():]
    c = _conv(s)
    if c:
        s = c[2]
    else:
        merged = _MERGE_PR_SUBJECT_RE.match(s)
        if merged:
            return "merged " + _branch_title(merged.group(1))
        mr = _MERGE_REQUEST_RE.match(s)
        if mr:
            return "merged " + _branch_title(mr.group(1))
    m = _TICKET_PREFIX_RE.match(s)
    if m and len(s) > m.end():
        s = s[m.end():]
    s = _TRAILING_PR_RE.sub("", s).strip()
    return s or (subject or "").strip()


def _branch_title(branch: str) -> str:
    """`dan/feature/add-login-ABC-123` -> `add login ABC-123`."""
    parts = [p for p in branch.split("/") if p]
    if len(parts) > 1 and parts[0].lower() not in _BRANCH_GROUPS:
        parts = parts[1:]                       # drop the user name
    if len(parts) > 1 and parts[0].lower() in _BRANCH_GROUPS:
        parts = parts[1:]
    return " ".join(re.split(r"[-_]+", "/".join(parts))).strip() or branch


def _branch_group(branch: str) -> str | None:
    for part in branch.split("/"):
        g = _BRANCH_GROUPS.get(part.lower())
        if g:
            return g
    return None


def classify_smart(subject: str, files=(), body: str = "") -> str:
    """The group (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`,
    `other`) a commit belongs to."""
    s = _WIP_TAG_RE.sub("", (subject or "").strip())
    m = _GITMOJI_RE.match(s)
    if m:
        g = _GITMOJI.get(m.group(0).strip())
        if g:
            return g
        s = s[m.end():]
    c = _conv(s)
    if c:
        return c[0]
    mp = _MERGE_PR_SUBJECT_RE.match(s) or _MERGE_REQUEST_RE.match(s)
    if mp:
        return _branch_group(mp.group(1)) or (
            "chore" if _BOT_RE.search(mp.group(1)) else "other")
    if _MERGE_BRANCH_RE.match(s):
        return "other"                      # a branch sync says nothing
    stripped = _TICKET_PREFIX_RE.sub("", s)
    stripped = _TRAILING_PR_RE.sub("", stripped).strip()
    words = re.findall(r"[A-Za-z']+", stripped.lower())
    if words:
        verb = _VERBS.get(words[0])
        if verb == "chore" and words[0] == "remove" and len(words) > 1:
            verb = None                         # decided by the files below
        if words[0] in ("update", "updates", "updated") and len(words) > 1:
            if words[1] in ("deps", "dependencies", "dependency", "lockfile"):
                return "chore"
            if words[1] in ("readme", "docs", "changelog", "documentation"):
                return "docs"
            if words[1] in ("test", "tests"):
                return "test"
        if verb == "fix":
            tail = " ".join(words[1:5]) if words[0].startswith("fix") else ""
            if re.search(r"\b(?:typos?|readme|docs?|changelog)\b", tail):
                return "docs"
            if re.search(r"\b(?:flaky|tests?|specs?)\b", tail):
                return "test"
            return "fix"
        if verb in ("docs", "test", "refactor"):
            return verb
        if words[0] in ("remove", "removes", "removed", "delete", "deletes",
                        "drop", "drops") and re.search(
                r"\b(?:dependency|dependencies|deps|package|packages)\b",
                stripped.lower()):
            return "chore"
        if verb == "feat":
            tail = " ".join(words[1:4])
            if re.search(r"\b(test|tests|spec)\b", tail):
                return "test"
            if re.search(r"\b(docs?|readme|changelog)\b", tail) \
                    or re.search(r"\bdocs?/", stripped.lower()):
                return "docs"
            if _CHORE_WORD_RE.search(stripped):
                return "chore"
            return "feat"
        if verb == "chore":
            return "chore"
    file_group = _files_group(files)
    if file_group:
        return file_group
    for rx, group in _PHRASES:
        if rx.search(stripped):
            return group
    return "other"


def _files_group(files) -> str | None:
    files = [f for f in files if f]
    if not files:
        return None
    if all(_DOC_PATH_RE.search(f) for f in files):
        return "docs"
    if all(_TEST_PATH_RE.search(f) for f in files):
        return "test"
    if all(_CHORE_PATH_RE.search(f) for f in files):
        return "chore"
    return None


def scope_of(subject: str) -> str:
    c = _conv((subject or "").strip())
    return c[1] if c else ""


# ---- folding -------------------------------------------------------------

@dataclasses.dataclass
class Item:
    """One line of the digest: a commit plus what was folded into it."""
    commit: object
    group: str
    text: str
    refs: refs_mod.Refs
    count: int = 1              # commits that say this (dedupe, fixups)


@dataclasses.dataclass
class Folded:
    items: list
    junk: int = 0               # bare wip / tmp / save commits
    syncs: int = 0              # "Merge branch 'main' into ..."
    fixups: int = 0             # fixup!/squash! folded into their target
    reverted: list = dataclasses.field(default_factory=list)

    def note(self) -> str:
        parts = []
        small = self.junk + self.fixups
        if small:
            parts.append(f"{small} fixup/WIP/save commit"
                         f"{'' if small == 1 else 's'}")
        if self.syncs:
            parts.append(f"{self.syncs} branch sync"
                         f"{'' if self.syncs == 1 else 's'}")
        if self.reverted:
            parts.append(f"{len(self.reverted)} revert pair"
                         f"{'' if len(self.reverted) == 1 else 's'}")
        return ("_Folded away: " + ", ".join(parts) + "._") if parts else ""


def _key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def fold(commits, *, do_fold: bool = True) -> Folded:
    """Turn commits (oldest or newest first, as given) into digest Items."""
    result = Folded(items=[])
    pending = []
    for c in commits:
        r = refs_mod.extract(c.subject, getattr(c, "body", ""))
        group = classify_smart(c.subject, c.files, getattr(c, "body", ""))
        pending.append(Item(c, group, clean_subject(c.subject), r))
    if not do_fold:
        result.items = pending
        return result

    keep = []
    fixups = []
    for it in pending:
        subj = it.commit.subject.strip()
        if _MERGE_BRANCH_RE.match(subj) and it.commit.pr_number is None \
                and not it.refs.prs:
            result.syncs += 1
            continue
        fm = _FIXUP_RE.match(subj)
        if fm:
            fixups.append((it, fm.group(2).strip()))
            continue
        if _JUNK_RE.match(clean_subject(subj)) and not it.refs:
            result.junk += 1
            continue
        keep.append(it)

    index: dict = {}
    for k in keep:
        index.setdefault(_key(k.text), k)
    for it, target in fixups:
        tk = _key(clean_subject(target))
        home = index.get(tk) if tk else None
        if home is not None:
            result.fixups += 1
        else:
            # the target is not in this window: the fixup is the news
            it.text = clean_subject(target) or it.text
            it.group = classify_smart(target, it.commit.files)
            keep.append(it)
            if tk:
                index[tk] = it

    # a revert and the commit it undoes cancel out
    by_key = {}
    for k in keep:
        by_key.setdefault(_key(k.text), []).append(k)
    dropped = set()
    for k in keep:
        rm = _REVERT_RE.match(k.commit.subject.strip())
        if not rm:
            continue
        for victim in by_key.get(_key(clean_subject(rm.group(1))), []):
            if victim is not k and id(victim) not in dropped:
                dropped.update((id(k), id(victim)))
                result.reverted.append(victim.text)
                break
    keep = [k for k in keep if id(k) not in dropped]

    # same subject on the same day: one line, "(x2)"
    seen = {}
    out = []
    for k in keep:
        sig = (k.commit.date, _key(k.text), k.group)
        first = seen.get(sig)
        if first is None:
            seen[sig] = k
            out.append(k)
        else:
            first.count += k.count
    result.items = out
    return result

"""Which commits were made with a coding agent, and (opt-in) which agent
sessions ran in this repository.

Commit flags are read from the commit itself: a `Co-authored-by:` trailer
naming an agent, the author name aider adds (`Name (aider)`), or the
footer some tools append (`Generated with Claude Code`). A flag says "an
agent helped", never "an agent wrote it all".

Session counts (`--agent-sessions`) read the JSONL transcripts Claude Code
keeps in `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects`). That file
format is the tool's own and unpublished, so the reader is defensive: a line
it does not understand is skipped, and nothing it reads is printed except
a count and the first words of each session's first request, which are run
through the redaction profile. It never leaves your computer and is off
unless you ask.
"""

from __future__ import annotations

import datetime
import json
import os
import re

#: (label, regex over a co-author "Name <email>" or the commit footer)
_AGENTS = (
    ("Claude", re.compile(r"\bclaude\b|@anthropic\.com", re.I)),
    ("Copilot", re.compile(r"copilot", re.I)),
    ("Codex", re.compile(r"\bcodex\b|@openai\.com", re.I)),
    ("Cursor", re.compile(r"cursoragent|cursor agent", re.I)),
    ("Devin", re.compile(r"devin-ai|\bdevin\b", re.I)),
    ("Gemini", re.compile(r"\bgemini\b", re.I)),
    ("Aider", re.compile(r"\(aider\)|\baider\b", re.I)),
)
_HINT_RE = re.compile(
    r"claude|anthropic|copilot|codex|openai|cursor|devin|gemini|aider", re.I)
_FOOTER_RE = re.compile(
    r"generated (?:with|by) \[?(claude code|codex|cursor|copilot|aider|devin)",
    re.I)


def agent_of(commit) -> str | None:
    """The agent's name if this commit shows one, else None."""
    candidates = list(getattr(commit, "coauthors", ()) or ())
    candidates.append(getattr(commit, "author", "") or "")
    body = getattr(commit, "body", "") or ""
    # fast path: nearly every commit names no agent at all
    if not _HINT_RE.search("\n".join(candidates) + "\n" + body):
        return None
    for label, rx in _AGENTS:
        if any(rx.search(c) for c in candidates):
            return label
    m = _FOOTER_RE.search(getattr(commit, "body", "") or "")
    if m:
        word = m.group(1).split()[0].lower()
        for label, _ in _AGENTS:
            if label.lower() == word:
                return label
    return None


def _same_place(cwd: str, root_real: str) -> bool:
    """Is a session's recorded working folder this repo? A recorded path
    the file system cannot spell (a non-ASCII folder under an ASCII locale)
    is simply not this repo, not a crash."""
    try:
        return os.path.normcase(os.path.realpath(cwd)) == root_real
    except (UnicodeError, OSError, ValueError):
        return False


def summary(commits) -> str:
    """`Agent-assisted: 3 of 9 commits (Claude 2, Copilot 1)` or ''."""
    counts: dict = {}
    for c in commits:
        a = agent_of(c)
        if a:
            counts[a] = counts.get(a, 0) + 1
    if not counts:
        return ""
    total = sum(counts.values())
    names = ", ".join(f"{k} {v}" for k, v in
                      sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"_Agent-assisted: {total} of {len(commits)} "
            f"commit{'' if len(commits) == 1 else 's'} ({names})_")


# ---- Claude Code transcripts (opt-in) ----------------------------------

MAX_LINES_PER_FILE = 50_000
MAX_FILES = 400


def _projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def _text_of(message) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" \
                    and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _local_day(stamp: str):
    try:
        dt = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone().date()


def sessions(root: str, first_day: datetime.date,
             last_day: datetime.date | None = None,
             projects_dir: str | None = None) -> list:
    """[{prompts: int, first: str}] for each transcript whose working
    directory is `root` and that has a request on a day in range."""
    base = projects_dir or _projects_dir()
    root_real = os.path.normcase(os.path.realpath(root))
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    files = 0
    for proj in names:
        pdir = os.path.join(base, proj)
        if not os.path.isdir(pdir):
            continue
        try:
            entries = sorted(os.listdir(pdir))
        except OSError:
            continue
        for fn in entries:
            if not fn.endswith(".jsonl"):
                continue
            files += 1
            if files > MAX_FILES:
                return found
            info = _read_session(os.path.join(pdir, fn), root_real,
                                 first_day, last_day)
            if info:
                found.append(info)
    return found


def _read_session(path, root_real, first_day, last_day):
    prompts, first, cwd_ok = 0, "", False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n >= MAX_LINES_PER_FILE:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                cwd = rec.get("cwd")
                if isinstance(cwd, str) and _same_place(cwd, root_real):
                    cwd_ok = True
                if rec.get("type") != "user" or rec.get("isSidechain") \
                        or rec.get("isMeta"):
                    continue
                text = _text_of(rec.get("message")).strip()
                if not text or text.startswith("<") or "tool_use_id" in text:
                    continue
                day = _local_day(rec.get("timestamp", ""))
                if day is None or day < first_day or (
                        last_day and day > last_day):
                    continue
                prompts += 1
                if not first:
                    first = " ".join(text.split())[:80]
    except OSError:
        return None
    return {"prompts": prompts, "first": first} if cwd_ok and prompts else None

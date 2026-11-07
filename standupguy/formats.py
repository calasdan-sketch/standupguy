"""The digest in the shape of the place it is going: plain text, Slack
mrkdwn, Microsoft Teams, or Markdown as it is. Plus the clipboard.

The conversion reads the Markdown standupguy itself writes (headings,
`- ` bullets, `` `code` ``, `_italic_`, `**bold**`), not arbitrary
Markdown. Every format neutralises mass mentions (`@channel`, `@here`,
`@everyone`, `<!channel>`) because a pasted standup can ping a whole
workspace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from . import push as push_mod

FORMATS = ("markdown", "plain", "slack", "teams")

_UNESCAPE_RE = re.compile(r"\\([<@(`])")


def _unescape(line: str) -> str:
    return _UNESCAPE_RE.sub(r"\1", line)


def _inline_plain(line: str) -> str:
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
    line = re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"\1", line)
    return line.replace("`", "")


def _mentions(line: str) -> str:
    return push_mod._MASS_MENTION_RE.sub(
        lambda m: "[at]" + (m.group(1) or m.group(2)).lower(), line)


def convert(markdown: str, fmt: str) -> str:
    """`markdown` (standupguy's own) in `fmt`."""
    if fmt == "markdown":
        return markdown
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    out = []
    for raw in markdown.split("\n"):
        line = raw
        if fmt != "teams":
            line = _unescape(line)
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            if fmt == "plain":
                line = title if level == 1 else (
                    f"{title.upper()}" if level == 2 else f"  {title}:")
            elif fmt == "slack":
                line = f"*{title}*" if level < 3 else f"_{title}_"
            else:       # teams
                line = f"**{title}**" if level < 3 else f"*{title}*"
        elif line.startswith("- "):
            body = line[2:]
            if fmt == "plain":
                line = "- " + _inline_plain(body)
            elif fmt == "slack":
                line = "• " + re.sub(r"\*\*(.+?)\*\*", r"*\1*", body)
            else:
                line = "- " + body
        else:
            if fmt == "plain":
                line = _inline_plain(line)
            elif fmt == "slack":
                line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        out.append(_mentions(line))
    text = "\n".join(out)
    if fmt == "slack":
        text = push_mod.sanitize_slack_text(text)
    return text


class ClipboardError(RuntimeError):
    pass


def clipboard_command(env=None, platform=None, which=shutil.which):
    """(argv, encoding) of the first clipboard tool found, else None."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if platform == "darwin":
        if which("pbcopy"):
            return ["pbcopy"], "utf-8"
        return None
    if platform in ("win32", "cygwin"):
        if which("clip"):
            return ["clip"], "utf-16"
        return None
    if env.get("WAYLAND_DISPLAY") and which("wl-copy"):
        return ["wl-copy"], "utf-8"
    if env.get("DISPLAY"):
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-in"], "utf-8"
        if which("xsel"):
            return ["xsel", "--clipboard", "--input"], "utf-8"
    if which("wl-copy"):
        return ["wl-copy"], "utf-8"
    if which("clip.exe"):                   # WSL
        return ["clip.exe"], "utf-16"
    return None


def copy(text: str, command=None) -> str:
    """Put `text` on the clipboard; return the tool's name. Raises
    ClipboardError with one line a person can act on."""
    found = command or clipboard_command()
    if not found:
        raise ClipboardError(
            "no clipboard tool found (macOS: pbcopy; Windows: clip; Linux: "
            "wl-copy, xclip or xsel) - print the text and copy it by hand")
    argv, encoding = found
    # `clip` wants UTF-16 with a byte-order mark; utf-16 adds it
    data = text.encode(encoding, "replace")
    try:
        subprocess.run(argv, input=data, timeout=10, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise ClipboardError(f"{argv[0]} disappeared from PATH") from None
    except subprocess.TimeoutExpired:
        raise ClipboardError(f"{argv[0]} did not finish within 10 seconds") \
            from None
    except subprocess.CalledProcessError as e:
        raise ClipboardError(f"{argv[0]} failed (exit {e.returncode})") \
            from None
    return os.path.basename(argv[0])

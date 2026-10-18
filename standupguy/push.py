"""Slack push: POST the notes to an incoming webhook. Opt-in only.

The webhook URL is the secret: whoever has it can post to the channel. No
error message repeats it, or any text of the HTTP library's own errors (they
quote the request path, which is where the token is).
"""

from __future__ import annotations

import http.client
import json
import re
import unicodedata
import urllib.request
import urllib.error

from . import stdio as stdio_mod

# Max characters per line of pushed text. Commit subjects flow through the
# digest and (optionally) the LLM draft into Slack, so a hostile commit
# message is an indirect prompt-injection vector against the team channel.
# Long commit-derived lines are truncated here before anything is posted.
MAX_PUSH_LINE_CHARS = 500

# Slack mass-mentions we refuse to forward verbatim. Both forms are covered:
# the plain @channel style and Slack's markup <!channel> style (which
# plain-@channel filters miss but Slack still renders as a mass mention),
# including Slack's optional display label (<!channel|channel>).
# The word boundary keeps us from rewriting words like @channeling.
_MASS_MENTION_RE = re.compile(
    r"<!((?:channel|here|everyone))(?:\|[^<>]*)?>"
    r"|@(channel|here|everyone)\b",
    re.IGNORECASE)


def sanitize_slack_text(text: str) -> str:
    """Neutralize risky content before it reaches the team Slack channel.

    - Strips control characters (keeps tab/newline) so hostile messages
      can't smuggle terminal escapes or invisible layout tricks.
    - Truncates each line to MAX_PUSH_LINE_CHARS (500) so a giant commit
      message can't become a giant injected payload.
    - Rewrites mass mentions as ``[at]channel``-style text: an injected
      ``@channel`` or ``<!channel>`` must never ping the whole team. We chose
      the visible ``[at]`` rewrite over a zero-width-space insertion because
      it is unambiguous in logs, diffs, and every Slack client (zero-width
      characters can be dropped or re-normalized downstream).
    - Escapes Slack's three markup characters (``&`` ``<`` ``>`` ->
      ``&amp;`` ``&lt;`` ``&gt;``, exactly the rule Slack's own docs give
      for message text). Everything in ``<...>`` is live markup to Slack:
      ``<@U024BE7LH>`` pings a person, ``<!subteam^S0123|@devs>`` pings a
      user group, ``<http://evil.example|the standup>`` disguises a link.
      Escaped, a commit subject can only ever render as literal text.
    """
    clean_lines = []
    for line in text.splitlines():
        line = "".join(
            ch for ch in line
            if ch == "\t" or unicodedata.category(ch) != "Cc"
        )
        if len(line) > MAX_PUSH_LINE_CHARS:
            line = line[:MAX_PUSH_LINE_CHARS] + " […]"
        line = _MASS_MENTION_RE.sub(
            lambda m: "[at]" + (m.group(1) or m.group(2)).lower(), line)
        line = escape_slack_markup(line)
        clean_lines.append(line)
    return "\n".join(clean_lines)


def escape_slack_markup(text: str) -> str:
    """Slack message-text escaping: ``&`` first, then ``<`` and ``>``.
    After this no ``<...>`` token survives, so no user/group mention,
    link or special command can be smuggled in from a commit message."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


class PushError(RuntimeError):
    pass


def push_slack(webhook_url: str, markdown: str, timeout: int = 30) -> None:
    """POST markdown to a Slack incoming webhook.

    The markdown is passed through :func:`sanitize_slack_text` first:
    control chars stripped, lines truncated at 500 chars, mass mentions
    (@channel/@here/@everyone and <!channel>/<!here>/<!everyone>)
    neutralized to ``[at]channel``-style text, and ``& < >`` escaped so
    Slack renders commit-derived text literally — no user, group or
    channel pings and no disguised links from a hostile commit message.
    """
    if not webhook_url.startswith("https://hooks.slack.com/"):
        raise PushError("refusing: not a Slack webhook URL")
    body = json.dumps({"text": sanitize_slack_text(markdown)}).encode()
    req = urllib.request.Request(webhook_url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201, 204):
                raise PushError(f"Slack returned HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        _let_go(e)
        raise PushError(f"Slack push failed: HTTP {e.code} "
                        f"{_plain(e.reason)}") from None
    except urllib.error.URLError as e:
        raise PushError(f"Slack push failed: {_plain(e.reason)}") from None
    except http.client.InvalidURL:
        raise PushError(
            "Slack push failed: the webhook URL holds a character a URL "
            "cannot carry (a space or a line break) - check it") from None
    except (OSError, http.client.HTTPException) as e:
        raise PushError(f"Slack push failed: {_plain(e)}") from None
    except ValueError:
        raise PushError("Slack push failed: the webhook URL cannot be used - "
                        "check it") from None


def _plain(reason, limit: int = 120) -> str:
    """What a library or a server said, as one printable line."""
    text = stdio_mod.printable(" ".join(str(reason).split()))
    return text[:limit] + ("..." if len(text) > limit else "")


def _let_go(error) -> None:
    """Close the reply an HTTPError carries (an open connection) now, not
    whenever the garbage collector gets to it. Other errors carry none. The
    error itself is closed, not only its file: Python 3.14 reports an
    HTTPError nobody closed, however closed its file is."""
    if getattr(error, "fp", None) is not None:
        error.close()

"""Draft: turn a digest into Yesterday/Today/Blockers prose via a local model.

Local-first: the default endpoint is Ollama on localhost. Point
AUTOSTANDUP_BASE_URL at freellmapi or any OpenAI-compatible endpoint.
Your git history never leaves the machine except to the endpoint YOU
configured. No cloud SaaS, no data-leak ceremony.

The API key goes to that endpoint and nowhere else: a redirect to another
address (scheme, host or port) is not followed, so a server cannot pass the
key on. And no error message repeats a secret: not the key, not the login
or query string of the endpoint's URL, not the text of an HTTP-library error
(which can hold the whole Authorization header).
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import stdio as stdio_mod


class DraftError(RuntimeError):
    pass


# Largest digest (characters) sent to the model for --draft. A full-repo
# digest can be hundreds of KB (the Round 2 panel measured 569KB/142k
# tokens on one repo), so the digest is truncated for the MODEL CALL only;
# the digest printed to the terminal is untouched.
MAX_DIGEST_CHARS = 100_000

TRUNCATION_NOTE = (
    "\n\n[Note: this digest was truncated to 100,000 characters for the "
    "model call. The full digest is still available above.]")


DRAFT_TEMPLATE = """You are writing a developer's daily standup notes. Be terse.
Use exactly these three sections:

Yesterday:
- 2-5 bullets of what was accomplished, in plain past tense.

Today:
- 2-4 bullets of what is in progress or planned next, inferred from the
  in-progress files and the trajectory of yesterday's work.

Blockers:
- "None reported." unless the digest clearly shows something stuck.

Rules: no fluff, no emojis, no inventing work that isn't in the digest.
If the digest is thin, say so in one line and stop.

Digest:
---
{digest}
---
"""


def _shown_url(url: str) -> str:
    """`url` as an error message may show it: without a login
    (``user:password@``), a query string or a fragment - those are where
    credentials live - and without control characters."""
    text = str(url).split("#", 1)[0].split("?", 1)[0]
    start = text.find("://") + 3 if "://" in text else 0
    slash = text.find("/", start)
    authority = text[start:] if slash < 0 else text[start:slash]
    if "@" in authority:
        text = text[:start] + text[start + authority.rindex("@") + 1:]
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def _shown_origin(url: str) -> str:
    """Just ``scheme://host:port`` of `url`, for saying where a redirect
    pointed without repeating a path or a query that may carry a token."""
    text = _shown_url(url)
    start = text.find("://") + 3 if "://" in text else 0
    slash = text.find("/", start)
    return text if slash < 0 else text[:slash]


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urllib.parse.urlsplit(url)
    scheme = (p.scheme or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    return scheme, (p.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _same_origin(a: str, b: str) -> bool:
    """Same scheme, host and port - ``https`` to ``http`` on the same host
    is a different origin, and so is another port."""
    return _origin(a) == _origin(b)


class _Redirected(Exception):
    def __init__(self, where: str):
        super().__init__(where)
        self.where = where


class _StayOnOrigin(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto the request it makes after
    a redirect, wherever the redirect points. A redirect to another origin is
    therefore refused BEFORE any request is made there: a server cannot make
    us hand the key (or the digest) to somebody else. A redirect within the
    origin is followed as urllib always did."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _same_origin(req.full_url, newurl):
            fp.close()      # urllib would have; we are not coming back
            raise _Redirected(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


NOT_PRINTABLE = ("a character that is not printable (a tab, a line break or "
                 "another invisible character)")
NOT_LATIN_1 = ("a character outside Latin-1 (a curly quote or an emoji, "
               "say), which an HTTP header cannot carry")


def _key_problem(key: str) -> str | None:
    """Why `key` cannot go into an Authorization header, or None. http.client
    puts a header on the wire as Latin-1 and refuses a line break in it - and
    its error message holds the whole header, key included."""
    if not key.isprintable():
        return NOT_PRINTABLE
    try:
        key.encode("latin-1")
    except UnicodeEncodeError:
        return NOT_LATIN_1
    return None


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


def _cfg(base_url=None, api_key=None, model=None):
    return {
        "base_url": (base_url or os.environ.get("AUTOSTANDUP_BASE_URL")
                     or "http://localhost:11434/v1").rstrip("/"),
        "api_key": api_key or os.environ.get("AUTOSTANDUP_API_KEY") or "ollama",
        "model": model or os.environ.get("AUTOSTANDUP_MODEL") or "llama3.1",
    }


def _cap_digest(digest_md: str) -> str:
    """Truncate the digest for the model call; the terminal copy is full.

    Returns the digest unchanged when it fits; otherwise keeps the first
    MAX_DIGEST_CHARS characters and appends a note saying so. The note is
    plain text without {} so DRAFT_TEMPLATE.format() stays safe.
    """
    if len(digest_md) <= MAX_DIGEST_CHARS:
        return digest_md
    return digest_md[:MAX_DIGEST_CHARS] + TRUNCATION_NOTE


def _snippet(raw: bytes, limit: int = 80) -> str:
    """What an endpoint said, folded onto one printable line for an error
    message (no newlines, no terminal escapes)."""
    text = " ".join(raw.decode("utf-8", "replace").split())
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text[:limit] + ("..." if len(text) > limit else "")


def draft(digest_md: str, *, base_url=None, api_key=None, model=None,
          timeout: int = 120) -> str:
    digest_md = _cap_digest(digest_md)
    cfg = _cfg(base_url, api_key, model)
    shown = _shown_url(cfg["base_url"])
    problem = _key_problem(cfg["api_key"])
    if problem:
        # said before anything is sent, and without repeating the key
        raise DraftError(f"the API key (AUTOSTANDUP_API_KEY) has {problem}; "
                         f"nothing was sent")
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user",
                      "content": DRAFT_TEMPLATE.format(digest=digest_md)}],
        "temperature": 0.3,
    }).encode()
    try:
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg['api_key']}"})
        opener = urllib.request.build_opener(_StayOnOrigin())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except _Redirected as e:
        raise DraftError(
            f"model at {shown} answered with a redirect to "
            f"{_shown_origin(e.where)}; it was not followed and your API key "
            f"was not sent there (set AUTOSTANDUP_BASE_URL to the final "
            f"address)") from None
    except urllib.error.URLError as e:
        _let_go(e)
        raise DraftError(
            f"cannot reach model at {shown} "
            f"(set AUTOSTANDUP_BASE_URL): {_plain(e.reason)}") from None
    except http.client.InvalidURL:
        # its message quotes the whole request path, query string included
        raise DraftError(
            f"cannot call the model at {shown}: the URL holds a character a "
            f"URL cannot carry (a space or a line break)") from None
    except (OSError, http.client.HTTPException) as e:
        # it answered, then hung up, timed out or sent less than it promised
        raise DraftError(
            f"model at {shown} stopped answering: {_plain(e)}") from None
    except ValueError:
        # Something the HTTP library would not put in the request. Its message
        # can hold the whole header ("Invalid header value b'Bearer <key>'"),
        # so it is left out - and not chained either.
        raise DraftError(
            f"cannot call the model at {shown}: the HTTP library refused the "
            f"request (its message is left out - it can hold your API key)"
        ) from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # not JSON (an HTML error page, cut off) or not UTF-8
        got = _snippet(raw)
        what = f"got: '{got}'" if got else "an empty reply"
        raise DraftError(f"model at {shown} answered, but not "
                         f"with JSON ({what})")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise DraftError(f"unexpected response shape: {e}")
    if not isinstance(content, str) or not content.strip():
        raise DraftError(f"model at {shown} returned no text")
    return content.strip()

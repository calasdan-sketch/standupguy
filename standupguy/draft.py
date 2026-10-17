"""Draft: turn a digest into Yesterday/Today/Blockers prose via a local model.

Local-first: the default endpoint is Ollama on localhost. Point
AUTOSTANDUP_BASE_URL at freellmapi or any OpenAI-compatible endpoint.
Your git history never leaves the machine except to the endpoint YOU
configured. No cloud SaaS, no data-leak ceremony.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.request
import urllib.error


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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        raise DraftError(
            f"cannot reach model at {cfg['base_url']} "
            f"(set AUTOSTANDUP_BASE_URL): {e.reason}")
    except (OSError, http.client.HTTPException) as e:
        # it answered, then hung up, timed out or sent less than it promised
        raise DraftError(f"model at {cfg['base_url']} stopped answering: {e}")
    except ValueError as e:  # a URL or API key that cannot be sent at all
        raise DraftError(f"cannot call the model at {cfg['base_url']}: {e}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:  # not JSON (an HTML error page, cut off) or not UTF-8
        got = _snippet(raw)
        what = f"got: '{got}'" if got else "an empty reply"
        raise DraftError(f"model at {cfg['base_url']} answered, but not "
                         f"with JSON ({what})")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise DraftError(f"unexpected response shape: {e}")
    if not isinstance(content, str) or not content.strip():
        raise DraftError(f"model at {cfg['base_url']} returned no text")
    return content.strip()

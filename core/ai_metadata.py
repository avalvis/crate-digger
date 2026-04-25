"""
core/ai_metadata.py
──────────────────────────────────────────────────────────────────────
Crate Digger — AI-Assisted Metadata Enrichment

Uses DeepSeek's chat completion API to extract the original artist name
and song title from YouTube video titles, which are often cluttered with
suffixes like "(slowed + reverb)", "[Official Audio]", or channel-specific
noise from unofficial re-upload channels.

The enricher is opt-in (Settings toggle) and requires DEEPSEEK_API_KEY
in the environment. When unavailable the pipeline falls back to the
existing heuristic (uploader channel name + raw video title).

API is OpenAI-compatible — the same request shape works against any
provider that implements the chat/completions endpoint, so swapping to
a different model is a one-line change.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Optional


_API_URL = "https://api.deepseek.com/chat/completions"
_MODEL   = "deepseek-chat"
_TIMEOUT = 12   # seconds — fast enough that it doesn't block the pipeline UX

_SYSTEM_PROMPT = (
    "You extract the original artist name and original song title from "
    "YouTube video metadata. Return ONLY a JSON object with two string "
    'fields: {"artist": "...", "title": "..."}. '
    "Rules:\n"
    "- 'artist' is the ORIGINAL recording artist, not a re-uploader channel.\n"
    "- 'title' is the ORIGINAL song title, stripped of modifiers such as "
    "'slowed', 'reverb', 'nightcore', 'remix', 'official audio', 'live', "
    "year stamps, etc.\n"
    "- If you are not confident about either field, return an empty string "
    "for that field — never guess.\n"
    "- Return no explanation, no markdown, only the JSON object."
)

# Regex to pull a JSON object from anywhere in the model's reply, in case
# it adds a stray sentence around the object despite the system prompt.
_JSON_RE = re.compile(r'\{[^{}]*"artist"[^{}]*"title"[^{}]*\}', re.DOTALL)


def make_ai_enricher(
    api_key: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[Callable[[str, str], tuple[str, str]]]:
    """
    Build and return an enricher callable, or None if no API key is available.

    The returned callable accepts (youtube_title, uploader) and returns
    (artist, title). Either string may be empty if the model isn't confident.
    On any network/parse failure the callable returns ("", "") so callers
    always have a safe fallback.
    """
    key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        return None

    log = logger or logging.getLogger("cratedigger.ai_metadata")

    def _enrich(youtube_title: str, uploader: str) -> tuple[str, str]:
        user_msg = (
            f'Video title: "{youtube_title}"\n'
            f'Uploader/channel name: "{uploader}"'
        )
        payload = {
            "model": _MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": 0.0,
            "max_tokens": 80,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

        try:
            import requests  # already in requirements.txt
            resp = requests.post(
                _API_URL,
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            log.warning("AI metadata call failed: %s", exc)
            return "", ""

        # Try direct parse first; fall back to regex extraction.
        artist, title = _parse_response(content)
        log.debug(
            "AI enrichment: title=%r uploader=%r → artist=%r title=%r",
            youtube_title, uploader, artist, title,
        )
        return artist, title

    return _enrich


def _parse_response(content: str) -> tuple[str, str]:
    """Extract (artist, title) from the model's reply string."""
    raw = content.strip()

    def _extract(data: dict) -> tuple[str, str]:
        artist = str(data.get("artist") or "").strip()
        title  = str(data.get("title")  or "").strip()
        return artist, title

    # Happy path: entire response is valid JSON.
    try:
        return _extract(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: find the first JSON object in the text.
    m = _JSON_RE.search(raw)
    if m:
        try:
            return _extract(json.loads(m.group()))
        except (json.JSONDecodeError, ValueError):
            pass

    return "", ""

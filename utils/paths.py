# utils/paths.py  — excerpt, full file delivered in later step
"""Filename / path sanitization helpers. Cross-platform safe."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import Path


# Characters invalid on Windows (and safer to avoid everywhere)
_INVALID_CHARS_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')

# Windows reserved device names
_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def sanitize_filename_component(
    s: str, *, max_length: int = 120, fallback: str = "Unknown",
) -> str:
    """
    Return a safe filename/directory component.

    Rules:
      • Unicode NFKC-normalized (collapses weird visual-equivalent glyphs)
      • Control chars and OS-invalid chars replaced with underscore
      • Leading/trailing dots and spaces stripped (Windows silently trims these)
      • Windows reserved names get an underscore suffix
      • Length-capped with graceful truncation
      • Empty/whitespace-only input → `fallback`
    """
    if not s:
        return fallback

    # NFKC: canonical equivalence. Turns full-width romaji, ligatures,
    # etc. into their standard forms so the vault doesn't accumulate
    # visually-identical-but-byte-distinct duplicates.
    s = unicodedata.normalize("NFKC", s)

    # Replace invalid chars with underscore
    s = _INVALID_CHARS_RE.sub("_", s)

    # Collapse runs of whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Strip leading/trailing dots AND spaces — Windows silently drops these
    s = s.strip(" .")

    if not s:
        return fallback

    # Dodge Windows reserved names
    if s.upper() in _WIN_RESERVED or s.upper().split(".")[0] in _WIN_RESERVED:
        s = f"{s}_"

    # Length cap. Truncate, then re-strip in case cap landed on a trailing dot.
    if len(s) > max_length:
        s = s[:max_length].rstrip(" .")
        if not s:
            return fallback

    return s


# Ordered list of (scheme_key, human_label) pairs for UI dropdowns.
# The first entry is the default used on fresh installs.
VAULT_FOLDER_SCHEMES: list[tuple[str, str]] = [
    ("date/artist_title",           "Date / Artist – Title  (recent-first default)"),
    ("genre/bpm_key_artist_title", "Genre / BPM · Key · Artist – Title"),
    ("artist/bpm_key_title",       "Artist / BPM · Key – Title"),
    ("genre/artist/bpm_key_title", "Genre / Artist / BPM · Key – Title"),
    ("bpm_key_artist_title",       "BPM · Key · Artist – Title  (flat)"),
    ("artist_title",               "Artist – Title  (simple)"),
]

_DEFAULT_SCHEME = VAULT_FOLDER_SCHEMES[0][0]


def build_vault_track_dir(
    vault_root: Path,
    *,
    genre: str | None,
    bpm: float | None,
    camelot_key: str | None,
    artist: str,
    title: str,
    scheme: str = _DEFAULT_SCHEME,
    filed_on: date | None = None,
) -> Path:
    """
    Construct the vault directory for one track according to `scheme`.

    Schemes
    -------
    date/artist_title           → YYYY-MM-DD/Artist_Title  (default)
    genre/bpm_key_artist_title  → Genre/BPM_Key_Artist_Title
    artist/bpm_key_title        → Artist/BPM_Key_Title
    genre/artist/bpm_key_title  → Genre/Artist/BPM_Key_Title
    bpm_key_artist_title        → BPM_Key_Artist_Title  (flat, no subfolder)
    artist_title                → Artist_Title  (simple)

    Every component is OS-sanitized. Unknown genre falls back to
    "Untagged" (not "Unknown") so it reads clearly in File Explorer.
    """
    san = sanitize_filename_component

    genre_dir  = san(genre or "Untagged", max_length=60)
    bpm_part   = f"{int(round(bpm))}" if bpm else "??"
    key_part   = san(camelot_key or "??", max_length=4, fallback="??")
    artist_part = san(artist, max_length=60)
    title_part  = san(title, max_length=80)

    bpm_key              = f"{bpm_part}_{key_part}"
    bpm_key_artist_title = san(f"{bpm_key}_{artist_part}_{title_part}", max_length=180)
    bpm_key_title        = san(f"{bpm_key}_{title_part}", max_length=180)
    artist_title         = san(f"{artist_part}_{title_part}", max_length=180)

    if scheme == "date/artist_title":
        date_part = (filed_on or date.today()).isoformat()
        return vault_root / date_part / artist_title
    if scheme == "artist/bpm_key_title":
        return vault_root / artist_part / bpm_key_title
    if scheme == "genre/artist/bpm_key_title":
        return vault_root / genre_dir / artist_part / bpm_key_title
    if scheme == "genre/bpm_key_artist_title":
        return vault_root / genre_dir / bpm_key_artist_title
    if scheme == "bpm_key_artist_title":
        return vault_root / bpm_key_artist_title
    if scheme == "artist_title":
        return vault_root / artist_title
    # Unknown scheme values fall back to the recent-first default.
    date_part = (filed_on or date.today()).isoformat()
    return vault_root / date_part / artist_title

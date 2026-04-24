# utils/paths.py  — excerpt, full file delivered in later step
"""Filename / path sanitization helpers. Cross-platform safe."""
from __future__ import annotations

import re
import unicodedata
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


def build_vault_track_dir(
    vault_root: Path,
    *,
    genre: str | None,
    bpm: float | None,
    camelot_key: str | None,
    artist: str,
    title: str,
) -> Path:
    """
    Construct `[vault_root]/[Genre]/[BPM]_[Key]_[Artist]_[Title]/` with
    every component OS-sanitized.
    """
    genre_dir = sanitize_filename_component(genre or "Unknown", max_length=60)

    bpm_part = f"{int(round(bpm))}" if bpm else "??"
    key_part = sanitize_filename_component(
        camelot_key or "??", max_length=4, fallback="??",
    )
    artist_part = sanitize_filename_component(artist, max_length=60)
    title_part = sanitize_filename_component(title, max_length=80)

    track_dir_name = f"{bpm_part}_{key_part}_{artist_part}_{title_part}"
    # Re-sanitize the whole assembled name to catch edge cases where
    # two sanitized components joined by "_" produce something weird.
    track_dir_name = sanitize_filename_component(track_dir_name, max_length=180)

    return vault_root / genre_dir / track_dir_name
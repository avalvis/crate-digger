"""
core/metadata.py
──────────────────────────────────────────────────────────────────────
Crate Digger — MP4/M4A Metadata Writer

Embeds artwork + standard and custom tags into the .m4a file's MP4
atom tree, using `mutagen`. Target compatibility matrix:

    • macOS Finder / iTunes / Music.app      — standard atoms
    • Serato DJ Pro / Rekordbox               — reads `----:com.apple.iTunes:INITIALKEY`
                                                and `tmpo` for BPM
    • MPC Software 2 / MPC Live / One+        — reads title, artist, album, covr
    • Android / generic MP4 tag readers       — standard atoms

Atom reference (the short names are the ones mutagen exposes):
    \xa9nam  — title
    \xa9ART  — artist
    \xa9alb  — album
    \xa9gen  — genre (standard string form)
    \xa9day  — year
    \xa9cmt  — comment
    \xa9grp  — grouping     (used by Rekordbox for key display)
    tmpo     — BPM (integer)
    covr    — cover art (MP4Cover objects)
    ----    — freeform key/value, namespaced (Serato reads these)

Design contract:
  • Idempotent: re-tagging the same file twice produces byte-identical
    tag structures (no duplicate covr, no stacked comments).
  • Never corrupts the file. On any write failure the original bytes
    are restored from an automatic .bak sibling and the error is raised.
  • Zero UI awareness. Zero network I/O.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, MP4StreamInfoError

# Optional import — `core.analyzer` will provide key/Camelot conversion
# but we don't want a hard dependency at import time. Local import at
# call sites keeps this module standalone-testable.


# ─── Public types ────────────────────────────────────────────────────

@dataclass(slots=True)
class TrackTags:
    """
    Denormalized tag bundle. All fields optional; writer skips `None`.

    This is the single interface `pipeline.py` writes against — it
    collects fields from Downloader (title/artist), Analyzer (BPM/key),
    and Discovery (genre/year/album) into this struct, then hands it
    to `MetadataWriter.apply()`.
    """
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    album_artist: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[int] = None
    comment: Optional[str] = None

    # DSP output
    bpm: Optional[float] = None            # mutagen stores `tmpo` as int
    musical_key: Optional[str] = None      # e.g. "Am", "F#"
    camelot_key: Optional[str] = None      # e.g. "8A"

    # Artwork — raw JPEG bytes from ArtworkProcessor
    artwork_jpeg: Optional[bytes] = None

    # Provenance
    source_url: Optional[str] = None


@dataclass(slots=True, frozen=True)
class MetadataWriteResult:
    file_path: Path
    bytes_before: int
    bytes_after: int
    fields_written: tuple[str, ...]
    artwork_embedded: bool


# ─── Public exceptions ───────────────────────────────────────────────

class MetadataError(Exception):
    """Base class for all errors raised by this module."""


class MetadataFileError(MetadataError):
    """The .m4a file could not be opened or is not a valid MP4."""


class MetadataWriteError(MetadataError):
    """Tag save failed. Original file has been restored from backup."""


# ─── Atom constants ──────────────────────────────────────────────────

# Standard iTunes-style atoms (4-char codes prefixed with \xa9 aka ©).
_ATOM_TITLE         = "\xa9nam"
_ATOM_ARTIST        = "\xa9ART"
_ATOM_ALBUM         = "\xa9alb"
_ATOM_ALBUM_ARTIST  = "aART"
_ATOM_GENRE         = "\xa9gen"
_ATOM_YEAR          = "\xa9day"
_ATOM_COMMENT       = "\xa9cmt"
_ATOM_GROUPING      = "\xa9grp"
_ATOM_BPM           = "tmpo"
_ATOM_COVER         = "covr"

# Freeform `----:mean:name` atoms. Serato and Rekordbox both read
# INITIALKEY from the com.apple.iTunes namespace — this is the
# industry-standard location for musical key in MP4 containers.
_ATOM_INITIAL_KEY   = "----:com.apple.iTunes:INITIALKEY"
_ATOM_CAMELOT_KEY   = "----:com.apple.iTunes:CAMELOT"
_ATOM_SOURCE_URL    = "----:com.crate-digger:SOURCE_URL"


# ─── The Writer ──────────────────────────────────────────────────────

class MetadataWriter:
    """
    Stateless facade over `mutagen.mp4.MP4`. Safe to share across
    worker threads — each `apply()` call opens and closes its own
    MP4 handle.
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._log = logger or logging.getLogger("cratedigger.metadata")

    # ── Public API ──

    def apply(self, file_path: Path, tags: TrackTags) -> MetadataWriteResult:
        """
        Embed `tags` into the .m4a at `file_path`. Atomic: on any
        failure, the original file is restored from a backup sibling.
        Existing tags are replaced (not merged) for every field the
        caller sets — unset fields are left untouched.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise MetadataFileError(f"File does not exist: {file_path}")
        if file_path.suffix.lower() not in (".m4a", ".mp4"):
            raise MetadataFileError(
                f"Unsupported extension {file_path.suffix!r}; expected .m4a/.mp4"
            )

        bytes_before = file_path.stat().st_size
        backup = file_path.with_suffix(file_path.suffix + ".bak")

        try:
            shutil.copy2(file_path, backup)
        except OSError as e:
            raise MetadataFileError(f"Could not create backup: {e}") from e

        try:
            audio = self._open(file_path)
            written = self._write_fields(audio, tags)
            try:
                audio.save()
            except (OSError, MP4StreamInfoError) as e:
                raise MetadataWriteError(f"mutagen save failed: {e}") from e

            bytes_after = file_path.stat().st_size

            # Success — remove backup.
            try:
                backup.unlink()
            except OSError:
                self._log.debug("Could not remove backup %s; leaving in place.", backup)

            self._log.debug(
                "Tagged %s: fields=%s artwork=%s size %d→%d bytes",
                file_path.name, written, tags.artwork_jpeg is not None,
                bytes_before, bytes_after,
            )
            return MetadataWriteResult(
                file_path=file_path,
                bytes_before=bytes_before,
                bytes_after=bytes_after,
                fields_written=tuple(written),
                artwork_embedded=tags.artwork_jpeg is not None,
            )
        except MetadataError:
            self._restore(backup, file_path)
            raise
        except Exception as e:
            self._restore(backup, file_path)
            raise MetadataWriteError(f"Unexpected tagging failure: {e}") from e

    def read(self, file_path: Path) -> dict[str, object]:
        """
        Introspect existing tags on a file. Used by pipeline reconcilers
        and by the Vault tab's 'inspect tags' debug panel. Returns a
        human-friendly dict — not the raw mutagen structures.
        """
        audio = self._open(Path(file_path))
        out: dict[str, object] = {}

        def first(atom: str) -> Optional[str]:
            v = audio.tags.get(atom) if audio.tags else None
            if not v:
                return None
            return str(v[0])

        if audio.tags:
            out["title"]         = first(_ATOM_TITLE)
            out["artist"]        = first(_ATOM_ARTIST)
            out["album"]         = first(_ATOM_ALBUM)
            out["album_artist"]  = first(_ATOM_ALBUM_ARTIST)
            out["genre"]         = first(_ATOM_GENRE)
            out["year"]          = first(_ATOM_YEAR)
            out["comment"]       = first(_ATOM_COMMENT)
            out["grouping"]      = first(_ATOM_GROUPING)

            bpm = audio.tags.get(_ATOM_BPM)
            out["bpm"] = int(bpm[0]) if bpm else None

            key_ff = audio.tags.get(_ATOM_INITIAL_KEY)
            out["musical_key"] = _decode_freeform(key_ff) if key_ff else None

            cam_ff = audio.tags.get(_ATOM_CAMELOT_KEY)
            out["camelot_key"] = _decode_freeform(cam_ff) if cam_ff else None

            covr = audio.tags.get(_ATOM_COVER)
            out["artwork_bytes"] = len(bytes(covr[0])) if covr else 0

        if audio.info:
            out["duration_seconds"] = float(audio.info.length or 0.0)
            out["bitrate_bps"]      = int(audio.info.bitrate or 0)
            out["sample_rate_hz"]   = int(audio.info.sample_rate or 0)
            out["channels"]         = int(audio.info.channels or 0)

        return out

    # ── Internal ──

    def _open(self, path: Path) -> MP4:
        try:
            audio = MP4(str(path))
        except MP4StreamInfoError as e:
            raise MetadataFileError(f"Not a valid MP4 container: {e}") from e
        except (OSError, ValueError) as e:
            raise MetadataFileError(f"Could not open {path}: {e}") from e

        if audio.tags is None:
            # File has no tag atom yet; add an empty one so assignment works.
            audio.add_tags()
        return audio

    def _write_fields(self, audio: MP4, t: TrackTags) -> list[str]:
        """
        Write each non-None field. Returns the list of atom codes
        actually modified — useful for logging and for the result struct.
        """
        written: list[str] = []

        def set_text(atom: str, value: Optional[str]) -> None:
            if value is None:
                return
            cleaned = value.strip()
            if not cleaned:
                return
            audio.tags[atom] = [cleaned]
            written.append(atom)

        set_text(_ATOM_TITLE,        t.title)
        set_text(_ATOM_ARTIST,       t.artist)
        set_text(_ATOM_ALBUM,        t.album)
        set_text(_ATOM_ALBUM_ARTIST, t.album_artist)
        set_text(_ATOM_GENRE,        t.genre)
        set_text(_ATOM_COMMENT,      t.comment)

        if t.year is not None:
            # MP4 year atom is a string; accept both int and str from caller.
            audio.tags[_ATOM_YEAR] = [str(int(t.year))]
            written.append(_ATOM_YEAR)

        if t.bpm is not None:
            # `tmpo` is an integer atom. Round half-to-even. Serato and
            # Rekordbox both read integer BPM here; they store decimal
            # BPM in their own private atoms which we don't touch.
            bpm_int = max(0, min(999, int(round(t.bpm))))
            audio.tags[_ATOM_BPM] = [bpm_int]
            written.append(_ATOM_BPM)

        if t.musical_key is not None:
            key = t.musical_key.strip()
            if key:
                # Grouping atom for Rekordbox's key display in the browser.
                audio.tags[_ATOM_GROUPING] = [key]
                written.append(_ATOM_GROUPING)
                # Freeform INITIALKEY for Serato / industry compatibility.
                audio.tags[_ATOM_INITIAL_KEY] = [
                    MP4FreeForm(key.encode("utf-8"))
                ]
                written.append(_ATOM_INITIAL_KEY)

        if t.camelot_key is not None:
            cam = t.camelot_key.strip()
            if cam:
                audio.tags[_ATOM_CAMELOT_KEY] = [
                    MP4FreeForm(cam.encode("utf-8"))
                ]
                written.append(_ATOM_CAMELOT_KEY)

        if t.source_url:
            audio.tags[_ATOM_SOURCE_URL] = [
                MP4FreeForm(t.source_url.encode("utf-8"))
            ]
            written.append(_ATOM_SOURCE_URL)

        if t.artwork_jpeg is not None:
            if len(t.artwork_jpeg) == 0:
                self._log.warning("Empty artwork buffer — skipping covr atom.")
            else:
                audio.tags[_ATOM_COVER] = [
                    MP4Cover(t.artwork_jpeg, imageformat=MP4Cover.FORMAT_JPEG)
                ]
                written.append(_ATOM_COVER)

        return written

    def _restore(self, backup: Path, target: Path) -> None:
        """Restore `target` from `backup` after a failed write."""
        if not backup.exists():
            return
        try:
            shutil.move(str(backup), str(target))
            self._log.warning("Restored %s from backup after tag failure.", target.name)
        except OSError as e:
            # We've done everything we can — log loudly and move on.
            self._log.error(
                "CRITICAL: could not restore %s from %s: %s",
                target, backup, e,
            )


# ─── Helpers ─────────────────────────────────────────────────────────

def _decode_freeform(value: object) -> Optional[str]:
    """
    Freeform atoms come back as a list of MP4FreeForm(bytes) or raw
    bytes depending on how they were written. Decode defensively.
    """
    try:
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, (bytes, bytearray, MP4FreeForm)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)
    except Exception:
        return None
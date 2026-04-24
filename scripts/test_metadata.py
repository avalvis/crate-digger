"""Manual smoke test for core/artwork.py + core/metadata.py."""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

from core.artwork import ArtworkProcessor, ArtworkError
from core.downloader import Downloader
from core.metadata import MetadataWriter, TrackTags

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
TEST_URL = "https://www.youtube.com/watch?v=ApXoWvfEYVU"


def main() -> int:
    dl = Downloader(ffmpeg_path=FFMPEG)
    art = ArtworkProcessor()
    meta = MetadataWriter()

    with tempfile.TemporaryDirectory(prefix="cratedigger_") as td:
        td_path = Path(td)

        # 1. Download
        print("── DOWNLOAD ──")
        result = dl.download(TEST_URL, td_path)
        print(f"  path       = {result.audio_path}")
        print(f"  size       = {result.audio_path.stat().st_size:,} bytes")
        print(f"  thumb_url  = {result.thumbnail_url}")

        # 2. Artwork
        print("\n── ARTWORK ──")
        if not result.thumbnail_url:
            print("  No thumbnail URL on result; skipping artwork test.")
            return 1
        try:
            artwork = art.fetch_and_process(result.thumbnail_url)
        except ArtworkError as e:
            print(f"  ARTWORK FAILED: {e}")
            return 1
        print(f"  dimensions     = {artwork.width}x{artwork.height}")
        print(f"  was_cropped    = {artwork.was_cropped}")
        print(f"  was_downscaled = {artwork.was_downscaled}")
        print(f"  jpeg bytes     = {len(artwork.data):,}")
        assert artwork.width == artwork.height, "artwork must be square"

        # 3. Tag
        print("\n── TAG ──")
        tags = TrackTags(
            title=result.track or result.title,
            artist=result.artist or result.uploader,
            album=result.album or "Crate Digger Test",
            genre="Test",
            year=2024,
            bpm=94.5,
            musical_key="Am",
            camelot_key="8A",
            comment="Crate Digger smoke test",
            source_url=result.source_url,
            artwork_jpeg=artwork.data,
        )
        write_result = meta.apply(result.audio_path, tags)
        print(f"  fields_written     = {write_result.fields_written}")
        print(f"  artwork_embedded   = {write_result.artwork_embedded}")
        print(f"  size {write_result.bytes_before:,} → {write_result.bytes_after:,} bytes")

        # 4. Round-trip
        print("\n── READBACK ──")
        readback = meta.read(result.audio_path)
        for k in ("title", "artist", "album", "genre", "year", "bpm",
                  "musical_key", "camelot_key", "duration_seconds",
                  "artwork_bytes"):
            print(f"  {k:20s} = {readback.get(k)!r}")

        assert readback["title"] == tags.title
        assert readback["artist"] == tags.artist
        assert readback["bpm"] == 95                 # rounded from 94.5
        assert readback["musical_key"] == "Am"
        assert readback["camelot_key"] == "8A"
        assert readback["artwork_bytes"] > 1000

        # 5. Idempotency — re-apply should not duplicate or corrupt
        print("\n── IDEMPOTENCY ──")
        write_result2 = meta.apply(result.audio_path, tags)
        readback2 = meta.read(result.audio_path)
        assert readback2["artwork_bytes"] == readback["artwork_bytes"]
        assert readback2["title"] == readback["title"]
        print(f"  re-tag size = {write_result2.bytes_after:,} bytes (unchanged: "
              f"{write_result2.bytes_after == write_result.bytes_after})")

        print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
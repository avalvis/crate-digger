"""End-to-end test for Digital Crate → MPC workflow (no GUI).

Run from project root:
    python scripts/test_mpc_workflow.py

Uses a short public YouTube clip, downloads preview audio, splits stems
with the fast MPC preset, and writes WAVs under a temp folder.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Short classic clip (~19s) — keeps CPU stem separation under a few minutes.
_TEST_VIDEO_ID = "jNQXAC9IVRw"
_TEST_ARTIST = "Test Artist"
_TEST_TITLE = "MPC Workflow Smoke Test"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )
    log = logging.getLogger("test.mpc_workflow")

    from utils.ffmpeg_setup import provision_ffmpeg
    from core.exporter import MPCExporter
    from core.mpc_export import export_sample_to_mpc
    from core.preview import PreviewService
    from core.stems import StemModel, StemSeparator

    binaries = provision_ffmpeg(logger=log)
    work = Path(tempfile.mkdtemp(prefix="crate_mpc_test_"))
    dest = work / "mpc_out"
    staging = work / "staging"
    cache = work / "preview_cache"
    dest.mkdir()
    staging.mkdir()
    cache.mkdir()

    log.info("Work dir: %s", work)

    preview = PreviewService(
        ffmpeg_path=binaries.ffmpeg_path,
        cache_dir=cache,
        logger=log.getChild("preview"),
    )
    stems = StemSeparator(
        model=StemModel.HTDEMUCS_FT,
        device="cpu",
        ffmpeg_path=binaries.ffmpeg_path,
        logger=log.getChild("stems"),
    )
    ok, info = stems.probe_runtime(timeout=30.0)
    if not ok:
        log.error("Stem runtime unavailable: %s", info)
        shutil.rmtree(work, ignore_errors=True)
        return 1

    exporter = MPCExporter(
        ffmpeg_path=binaries.ffmpeg_path,
        logger=log.getChild("exporter"),
    )

    def on_progress(label: str, pct: float) -> None:
        log.info("  [%5.1f%%] %s", pct, label)

    try:
        log.info("Fetching preview for %s …", _TEST_VIDEO_ID)
        preview.fetch(_TEST_VIDEO_ID)

        log.info("Running MPC export …")
        result = export_sample_to_mpc(
            video_id=_TEST_VIDEO_ID,
            artist=_TEST_ARTIST,
            title=_TEST_TITLE,
            destination_root=dest,
            staging_root=staging,
            preview=preview,
            stem_separator=stems,
            exporter=exporter,
            progress_callback=on_progress,
            logger=log,
        )
    except Exception as e:
        log.exception("MPC workflow test failed: %s", e)
        shutil.rmtree(work, ignore_errors=True)
        return 1

    wavs = sorted(result.track_dir.glob("*.wav"))
    if len(wavs) < 4:
        log.error("Expected 4 stem WAVs, got %d in %s", len(wavs), result.track_dir)
        shutil.rmtree(work, ignore_errors=True)
        return 1

    for wav in wavs:
        size = wav.stat().st_size
        if size < 1000:
            log.error("Stem file too small: %s (%d bytes)", wav.name, size)
            shutil.rmtree(work, ignore_errors=True)
            return 1
        log.info("  OK  %s  (%d bytes)", wav.name, size)

    log.info("PASS — stems at %s", result.track_dir)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Manual smoke test for core/analyzer.py + core/stems.py."""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from core.analyzer import (
    AudioAnalyzer, AnalysisProgress, AnalyzerError,
)
from core.downloader import Downloader
from core.stems import (
    SeparationProgress, StemModel, StemSeparationError, StemSeparator,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
TEST_URL = "https://www.youtube.com/watch?v=ApXoWvfEYVU"


def analyzer_progress(p: AnalysisProgress) -> None:
    print(f"  [{p.stage.value:<22}] {p.percent:5.1f}%  {p.message}")


def stems_progress(p: SeparationProgress) -> None:
    print(f"  [{p.stage.value:<18}] {p.percent:5.1f}%  "
          f"t+{p.elapsed_seconds:5.1f}s  {p.message}")


def main() -> int:
    dl = Downloader(ffmpeg_path=FFMPEG)

    with tempfile.TemporaryDirectory(prefix="cratedigger_") as td:
        td_path = Path(td)

        # 1. Download (needed as test fixture)
        print("── DOWNLOAD ──")
        dl_result = dl.download(TEST_URL, td_path)
        print(f"  path = {dl_result.audio_path}")

        # 2. Analyze
        print("\n── ANALYZE ──")
        analyzer = AudioAnalyzer()
        t0 = time.monotonic()
        try:
            analysis = analyzer.analyze(
                dl_result.audio_path,
                progress_callback=analyzer_progress,
            )
        except AnalyzerError as e:
            print(f"  ANALYZE FAILED: {e}")
            return 1
        analyze_elapsed = time.monotonic() - t0

        print(f"\n  bpm            = {analysis.bpm} (conf={analysis.bpm_confidence:.2f})")
        print(f"  musical_key    = {analysis.musical_key}")
        print(f"  camelot_key    = {analysis.camelot_key} (conf={analysis.key_confidence:.2f})")
        print(f"  duration       = {analysis.duration_seconds:.1f}s")
        print(f"  analysis_time  = {analyze_elapsed:.2f}s")

        assert 40 < analysis.bpm < 220, f"BPM out of plausible range: {analysis.bpm}"
        assert analysis.camelot_key[-1] in ("A", "B"), "Camelot ring letter invalid"
        assert analysis.camelot_key[:-1].isdigit(), "Camelot number invalid"

        # 3. Analyzer speed check (re-run without HPSS)
        print("\n── ANALYZER (no HPSS) ──")
        fast = AudioAnalyzer(hpss_margin=None)
        t0 = time.monotonic()
        fast_result = fast.analyze(dl_result.audio_path)
        print(f"  bpm={fast_result.bpm}  key={fast_result.musical_key}  "
              f"elapsed={time.monotonic() - t0:.2f}s "
              f"(should be meaningfully faster than HPSS path)")

        # 4. Stems availability
        print("\n── STEMS AVAILABILITY ──")
        separator = StemSeparator(model=StemModel.HTDEMUCS_FT)
        available, info = separator.probe_availability()
        print(f"  available = {available}")
        print(f"  info      = {info}")
        if not available:
            print("  Skipping separation test — demucs/torch not installed.")
            return 0

        # 5. Separate (real run — this takes 30–180 seconds)
        print("\n── SEPARATE (htdemucs_ft) ──")
        print("  This may take 30–180 seconds depending on CPU/GPU...")
        stems_out_dir = td_path / "stems"
        try:
            stems_result = separator.separate(
                dl_result.audio_path,
                stems_out_dir,
                progress_callback=stems_progress,
            )
        except StemSeparationError as e:
            print(f"  SEPARATION FAILED: {e}")
            return 1

        print(f"\n  model_used  = {stems_result.model_used}")
        print(f"  device_used = {stems_result.device_used}")
        print(f"  elapsed     = {stems_result.elapsed_seconds:.1f}s")
        for name, path in stems_result.stems.items():
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"    {name:<7} {size_mb:6.2f} MB  {path}")

        for name in ("vocals", "drums", "bass", "other"):
            assert name in stems_result.stems, f"Missing stem: {name}"
            assert stems_result.stems[name].exists(), f"Stem file missing: {name}"
            assert stems_result.stems[name].stat().st_size > 100_000, \
                f"Stem file suspiciously small: {name}"

        # 6. Verify staging cleanup
        assert not (stems_out_dir / "_demucs_staging").exists(), \
            "Staging dir not cleaned up"

        print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
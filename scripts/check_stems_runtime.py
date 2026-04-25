"""Quick environment check for Demucs stem runtime.

Run:
    python scripts/check_stems_runtime.py

Exit codes:
    0 -> runtime healthy
    1 -> runtime unavailable / broken
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from core.stems import StemModel, StemSeparator
from utils.ffmpeg_setup import FFmpegProvisioningError, provision_ffmpeg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


def main() -> int:
    try:
        bins = provision_ffmpeg(logger=logging.getLogger("check.ffmpeg"))
    except FFmpegProvisioningError as e:
        print(f"FAIL: ffmpeg provisioning failed: {e}")
        return 1

    sep = StemSeparator(
        model=StemModel.HTDEMUCS,
        device="cpu",
        ffmpeg_path=str(Path(bins.ffmpeg_path)),
        logger=logging.getLogger("check.stems"),
    )

    ok, info = sep.probe_runtime(timeout=25.0)
    if ok:
        print(f"OK: stem runtime healthy: {info}")
        return 0

    print(f"FAIL: stem runtime unavailable: {info}")
    if (
        "libtorchcodec_core" in info.lower()
        or "could not load this library" in info.lower()
    ):
        print("Hint: torchcodec native DLL failed to load.")
        print(
            "Hint: install/repair Microsoft Visual C++ Redistributable x64 (2015-2022), then reopen terminal."
        )
    else:
        print(
            "Hint: reinstall pinned deps and ensure MSVC Redistributable x64 is present."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())

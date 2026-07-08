"""
scripts/preflight_check.py
──────────────────────────
Quick environment check before launching Crate Digger.

Exit codes:
  0  — core dependencies OK (ready to run)
  1  — missing packages (run pip install -r requirements.txt)
  2  — Python version too old (< 3.11)
"""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 11)

# Core runtime imports — must succeed for the GUI + pipeline to start.
_CORE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("customtkinter", "customtkinter"),
    ("pydantic", "pydantic"),
    ("yt_dlp", "yt-dlp"),
    ("mutagen", "mutagen"),
    ("librosa", "librosa"),
    ("numpy", "numpy"),
    ("requests", "requests"),
    ("ytmusicapi", "ytmusicapi"),
    ("sounddevice", "sounddevice"),
    ("PIL", "Pillow"),
    ("imageio_ffmpeg", "imageio-ffmpeg"),
)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(
            f"ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required; "
            f"found {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            file=sys.stderr,
        )
        return 2

    missing: list[str] = []
    for module, pip_name in _CORE_IMPORTS:
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print("MISSING:", ", ".join(missing), file=sys.stderr)
        return 1

    # Stems are optional — warn but do not block launch.
    try:
        import torch  # noqa: F401
        import demucs  # noqa: F401
    except ImportError:
        print(
            "NOTE: Stem separation deps not installed (torch/demucs). "
            "Ingestion works; enable stems only after full install.",
        )

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

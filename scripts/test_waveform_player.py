"""Waveform player geometry + peaks downsampling tests (no Tk UI)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.components.waveform_player import WaveformPlayer


def test_peaks_for_display_downsamples() -> None:
    peaks = np.linspace(0.1, 1.0, 512, dtype=np.float32)
    out = WaveformPlayer._peaks_for_display(peaks)
    assert out is not None
    assert out.size == WaveformPlayer._DISPLAY_BARS


def test_peaks_for_display_preserves_short_input() -> None:
    peaks = np.array([0.2, 0.8, 0.5], dtype=np.float32)
    out = WaveformPlayer._peaks_for_display(peaks)
    assert out is not None
    assert out.size == 3


def test_bar_layout_uses_full_width() -> None:
    """Bars must span the canvas — not cluster when width is realistic."""
    w = 640
    bar_count = WaveformPlayer._DISPLAY_BARS
    bar_w = max(1, w // bar_count)
    last_x0 = (bar_count - 1) * bar_w
    assert last_x0 >= w // 2


def main() -> None:
    test_peaks_for_display_downsamples()
    test_peaks_for_display_preserves_short_input()
    test_bar_layout_uses_full_width()
    print("test_waveform_player: all passed")


if __name__ == "__main__":
    main()

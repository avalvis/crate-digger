"""
core/chopper.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Chop / Loop Intelligence

Segments a track into MPC-ready chop points and bar-aligned loop regions
using onset detection plus the rhythm grid from AudioAnalyzer.

Typical workflow:
  analysis = analyzer.analyze(path)
  plan = chopper.plan(path, analysis)
  exporter.export_chop_kit(path, plan, destination)

Thread safety: stateless across calls; safe to share across workers.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from core.analyzer import AnalysisCancelledError, AnalysisResult, AudioAnalyzer


# ─── Public types ────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class ChopPoint:
    """A suggested slice boundary (transient / hit)."""

    time_seconds: float
    strength: float = 0.0  # 0 = weak transient, 1 = strong hit
    label: str = ""


@dataclass(slots=True, frozen=True)
class LoopRegion:
    """A bar-aligned loop region suitable for MPC pad assignment."""

    start_seconds: float
    end_seconds: float
    bars: int
    confidence: float = 0.0


@dataclass(slots=True)
class ChopPlan:
    """Full chop + loop suggestion for one track."""

    source_path: Path
    bpm: float
    beats_per_bar: int
    chop_points: list[ChopPoint] = field(default_factory=list)
    loop_regions: list[LoopRegion] = field(default_factory=list)
    one_shot_regions: list[tuple[float, float]] = field(default_factory=list)
    # (start, end) pairs for isolated hits between chops


# ─── Public exceptions ───────────────────────────────────────────────


class ChopperError(Exception):
    """Base class for chopper failures."""


# ─── The Chopper ─────────────────────────────────────────────────────


class AudioChopper:
    """
    Detects transient chop points and bar-aligned loops from audio +
    an existing AnalysisResult (BPM / downbeat grid).
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        *,
        analyzer: Optional[AudioAnalyzer] = None,
        min_chop_gap_seconds: float = 0.08,
        max_chops: int = 64,
        loop_bar_lengths: tuple[int, ...] = (1, 2, 4),
        one_shot_max_seconds: float = 2.5,
    ) -> None:
        self._log = logger or logging.getLogger("cratedigger.chopper")
        self._analyzer = analyzer or AudioAnalyzer(logger=self._log)
        self._min_gap = float(min_chop_gap_seconds)
        self._max_chops = int(max_chops)
        self._loop_bars = loop_bar_lengths
        self._one_shot_max = float(one_shot_max_seconds)

    def plan(
        self,
        audio_path: Path,
        analysis: Optional[AnalysisResult] = None,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ChopPlan:
        """
        Build a ChopPlan for `audio_path`. Runs analysis when `analysis`
        is not supplied.
        """
        path = Path(audio_path)
        if not path.exists():
            raise ChopperError(f"File does not exist: {path}")

        if analysis is None:
            if progress_callback:
                progress_callback("Analyzing audio")
            analysis = self._analyzer.analyze(
                path, cancel_event=cancel_event,
            )

        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisCancelledError("Cancelled by user.")

        if progress_callback:
            progress_callback("Detecting chop points")

        import librosa

        y, sr = self._analyzer._load_audio(path)  # noqa: SLF001 — shared loader
        chops = self._detect_chops(y, sr, librosa, analysis.duration_seconds)

        if progress_callback:
            progress_callback("Finding bar-aligned loops")

        loops = self._detect_loops(analysis)
        one_shots = self._chops_to_one_shots(
            chops, analysis.duration_seconds,
        )

        self._log.debug(
            "Chop plan for %s: %d chops, %d loops, %d one-shots @ %.1f BPM",
            path.name, len(chops), len(loops), len(one_shots), analysis.bpm,
        )

        return ChopPlan(
            source_path=path,
            bpm=analysis.bpm,
            beats_per_bar=analysis.beats_per_bar,
            chop_points=chops,
            loop_regions=loops,
            one_shot_regions=one_shots,
        )

    def _detect_chops(
        self,
        y: np.ndarray,
        sr: int,
        librosa,
        duration: float,
    ) -> list[ChopPoint]:
        """Onset-based transient detection with de-duplication."""
        if y.size == 0 or duration <= 0:
            return []

        onset_env = librosa.onset.onset_strength(y=y, sr=sr, aggregate=np.median)
        peaks = librosa.util.peak_pick(
            onset_env,
            pre_max=3,
            post_max=3,
            pre_avg=3,
            post_avg=5,
            delta=0.07,
            wait=int(sr * self._min_gap / 512),
        )

        hop = 512
        points: list[ChopPoint] = []
        env_max = float(np.max(onset_env)) or 1.0

        for frame in peaks:
            t = float(librosa.frames_to_time(frame, sr=sr, hop_length=hop))
            if t < 0 or t >= duration:
                continue
            strength = float(onset_env[frame]) / env_max
            points.append(ChopPoint(time_seconds=t, strength=strength))

        # Sort by strength, keep strongest up to max_chops, re-sort by time.
        points.sort(key=lambda p: p.strength, reverse=True)
        points = points[: self._max_chops]
        points.sort(key=lambda p: p.time_seconds)

        # Enforce minimum gap after strength filtering.
        filtered: list[ChopPoint] = []
        last_t = -1.0
        for p in points:
            if p.time_seconds - last_t >= self._min_gap:
                filtered.append(p)
                last_t = p.time_seconds

        # Number chops for pad labels.
        return [
            ChopPoint(
                time_seconds=p.time_seconds,
                strength=p.strength,
                label=f"chop_{i + 1:02d}",
            )
            for i, p in enumerate(filtered)
        ]

    def _detect_loops(self, analysis: AnalysisResult) -> list[LoopRegion]:
        """Suggest 1/2/4-bar loops anchored on downbeats."""
        if analysis.bpm <= 0 or analysis.bar_period <= 0:
            return []

        downbeats = list(analysis.downbeat_times)
        if not downbeats and analysis.beat_times:
            # Fall back to every Nth beat as a pseudo-downbeat.
            n = max(1, analysis.beats_per_bar)
            downbeats = list(analysis.beat_times[::n])

        if len(downbeats) < 2:
            return []

        loops: list[LoopRegion] = []
        bar_period = analysis.bar_period

        for bars in self._loop_bars:
            span = bar_period * bars
            best: Optional[LoopRegion] = None
            for start in downbeats:
                end = start + span
                if end > analysis.duration_seconds:
                    break
                # Prefer loops that start early in the track (intro hooks).
                position_score = 1.0 - min(1.0, start / max(analysis.duration_seconds, 1.0))
                region = LoopRegion(
                    start_seconds=start,
                    end_seconds=end,
                    bars=bars,
                    confidence=round(0.5 + 0.5 * position_score, 3),
                )
                if best is None or region.confidence > best.confidence:
                    best = region
            if best is not None:
                loops.append(best)

        return loops

    def _chops_to_one_shots(
        self,
        chops: list[ChopPoint],
        duration: float,
    ) -> list[tuple[float, float]]:
        """
        Convert adjacent chop points into one-shot slice windows.
        Each slice runs from one chop to the next (capped at max length).
        """
        if not chops:
            return []

        regions: list[tuple[float, float]] = []
        times = [p.time_seconds for p in chops]

        for i, start in enumerate(times):
            if i + 1 < len(times):
                end = min(times[i + 1], start + self._one_shot_max)
            else:
                end = min(duration, start + self._one_shot_max)
            if end - start >= self._min_gap:
                regions.append((start, end))

        return regions

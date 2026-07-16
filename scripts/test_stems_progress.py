"""Fast unit coverage for Demucs progress aggregation and run locking."""

from __future__ import annotations

import io
import threading
import time
from unittest.mock import patch

import pytest

from core.stems import (
    SeparationProgress,
    SeparationStage,
    StemModel,
    StemSeparationCancelledError,
    StemSeparator,
)


class _FinishedProcess:
    def __init__(self, lines: list[str]) -> None:
        self.stdout = io.StringIO("\n".join(lines) + "\n")

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _run_with_output(lines: list[str]) -> list[SeparationProgress]:
    separator = StemSeparator()
    events: list[SeparationProgress] = []
    process = _FinishedProcess(lines)
    with patch("core.stems.subprocess.Popen", return_value=process):
        separator._run_demucs(  # noqa: SLF001 - focused parser unit test
            ["fake-demucs"],
            events.append,
            cancel_event=None,
            started=time.monotonic(),
        )
    return events


def test_four_model_ensemble_progress_is_aggregated() -> None:
    lines = [
        "100%| model download",  # Must not be mistaken for separation work.
        "Selected model is a bag of 4 models. You will see that many progress bars per track.",
        "Separating track example.m4a",
    ]
    for _ in range(4):
        lines.extend(("0%| pass", "50%| pass", "100%| pass", "100%| pass"))

    events = _run_with_output(lines)
    separating = [e for e in events if e.stage is SeparationStage.SEPARATING]

    assert separating
    assert [e.percent for e in separating] == sorted(e.percent for e in separating)
    assert separating[-1].percent == pytest.approx(95.0)
    assert any("model 1/4" in e.message for e in separating)
    assert any("model 4/4" in e.message for e in separating)
    # The first pass is only one quarter of the actual Demucs work.
    first_pass_done = next(e for e in separating if e.message == "Separating model 1/4 (100%)")
    assert first_pass_done.percent == pytest.approx(31.25)


def test_single_model_progress_keeps_standard_mapping() -> None:
    events = _run_with_output([
        "Selected model is a bag of 1 models.",
        "Separating track example.m4a",
        "0%| pass",
        "100%| pass",
    ])
    separating = [e for e in events if e.stage is SeparationStage.SEPARATING]

    assert [e.percent for e in separating] == pytest.approx([10.0, 95.0])
    assert separating[-1].message == "Separating (100%)"


def test_waiting_for_run_slot_is_cancellable() -> None:
    separator = StemSeparator(model=StemModel.HTDEMUCS)
    separator._run_lock.acquire()  # noqa: SLF001 - exercise lock boundary
    cancelled = threading.Event()
    cancelled.set()
    try:
        with pytest.raises(StemSeparationCancelledError, match="waiting for stems"):
            separator._acquire_run_slot(None, cancelled)  # noqa: SLF001
    finally:
        separator._run_lock.release()  # noqa: SLF001


def test_balanced_model_is_the_default() -> None:
    separator = StemSeparator()
    assert separator._model is StemModel.HTDEMUCS  # noqa: SLF001

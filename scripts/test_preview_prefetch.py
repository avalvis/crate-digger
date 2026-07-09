"""Smoke test for PreviewPrefetchService lifecycle (no network)."""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.preview import PreviewData, PreviewService
from core.preview_prefetch import (
    PrefetchEventType,
    PrefetchState,
    PreviewPrefetchService,
)


class PreviewPrefetchSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._preview = PreviewService(
            ffmpeg_path="ffmpeg",
            cache_dir=Path(self._tmpdir.name),
            logger=logging.getLogger("test.preview"),
        )
        self._service = PreviewPrefetchService(
            self._preview,
            max_workers=1,
            keep_decoded=True,
            logger=logging.getLogger("test.prefetch"),
        )
        self._service.start()
        self._events: list = []
        self._service.subscribe(self._events.append, weak=False)

    def tearDown(self) -> None:
        self._service.shutdown(cancel_pending=True)
        self._tmpdir.cleanup()

    def test_enqueue_dedupes_and_reaches_ready(self) -> None:
        fake_data = PreviewData(
            video_id="abc123",
            samplerate=44100,
            channels=2,
            samples=__import__("numpy").zeros((1000, 2), dtype="float32"),
            peaks=__import__("numpy").zeros(200, dtype="float32"),
            duration_seconds=1.0,
            source_path=Path("x.m4a"),
            is_partial=True,
        )

        with patch.object(
            self._preview, "warm_cache", return_value=Path("x.m4a"),
        ), patch.object(
            self._preview, "fetch_quick", return_value=fake_data,
        ):
            self._service.enqueue_batch(["abc123", "abc123"])
            deadline = __import__("time").monotonic() + 5.0
            while __import__("time").monotonic() < deadline:
                if self._service.get_state("abc123") == PrefetchState.READY:
                    break
                __import__("time").sleep(0.05)

        self.assertEqual(self._service.get_state("abc123"), PrefetchState.READY)
        self.assertIsNotNone(self._service.get_decoded("abc123"))
        types = {e.type for e in self._events}
        self.assertIn(PrefetchEventType.STATE_CHANGED, types)

    def test_cancel_batch(self) -> None:
        started = threading.Event()

        def slow_warm(*_a, **_k):
            started.set()
            __import__("time").sleep(2.0)
            return Path("x.m4a")

        with patch.object(self._preview, "warm_cache", side_effect=slow_warm):
            self._service.enqueue_batch(["slowvid"])
            deadline = __import__("time").monotonic() + 2.0
            while not started.is_set() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
            self._service.cancel_batch(["slowvid"])
            state = self._service.get_state("slowvid")
            self.assertIn(state, (PrefetchState.CANCELLED, PrefetchState.FAILED))


if __name__ == "__main__":
    unittest.main()

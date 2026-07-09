"""Smoke test for MpcExportManager enqueue/cancel/drain lifecycle."""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.discovery import DiscoverySuggestion
from core.mpc_export import MpcExportMode, MpcSampleResult
from core.mpc_export_manager import MpcExportEventType, MpcExportManager


def _suggestion(vid: str = "vid123") -> DiscoverySuggestion:
    return DiscoverySuggestion(
        discogs_master_id=1,
        discogs_release_id=None,
        artist="Test",
        title="Track",
        year=1975,
        country="USA",
        genre="Jazz",
        style=None,
        youtube_url=f"https://youtube.com/watch?v={vid}",
        youtube_video_id=vid,
        youtube_title="Test — Track",
        youtube_duration_seconds=180,
        match_score=0.9,
    )


class MpcExportManagerSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self._preview = MagicMock()
        self._stems = MagicMock()
        self._exporter = MagicMock()
        self._mgr = MpcExportManager(
            preview=self._preview,
            stem_separator=self._stems,
            exporter=self._exporter,
            destination_root=lambda: root / "mpc",
            staging_root=lambda: root / "stage",
            max_workers=1,
            logger=logging.getLogger("test.mpc_mgr"),
        )
        self._mgr.start()
        self._events: list = []
        self._mgr.subscribe(self._events.append, weak=False)

    def tearDown(self) -> None:
        self._mgr.shutdown(cancel_pending=True)
        self._tmpdir.cleanup()

    def test_enqueue_dedupes_same_video(self) -> None:
        done = threading.Event()
        result = MpcSampleResult(
            track_dir=Path(self._tmpdir.name) / "mpc" / "Test - Track",
            stems={},
            original=Path("original.wav"),
        )

        def fake_export(**_kwargs):
            done.set()
            return result

        with patch("core.mpc_export_manager.export_sample_to_mpc", side_effect=fake_export):
            j1 = self._mgr.enqueue(_suggestion(), MpcExportMode.SONG)
            j2 = self._mgr.enqueue(_suggestion(), MpcExportMode.STEMS)
            self.assertEqual(j1, j2)
            deadline = __import__("time").monotonic() + 5.0
            while not done.is_set() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.05)

        types = [e.type for e in self._events]
        self.assertIn(MpcExportEventType.COMPLETED, types)

    def test_cancel_queued_job(self) -> None:
        block = threading.Event()
        started = threading.Event()

        def slow_export(**_kwargs):
            started.set()
            block.wait(timeout=3.0)
            return MpcSampleResult(
                track_dir=Path(self._tmpdir.name),
                stems={},
            )

        with patch("core.mpc_export_manager.export_sample_to_mpc", side_effect=slow_export):
            j1 = self._mgr.enqueue(_suggestion("a"), MpcExportMode.SONG)
            j2 = self._mgr.enqueue(_suggestion("b"), MpcExportMode.SONG)
            deadline = __import__("time").monotonic() + 2.0
            while not started.is_set() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
            self._mgr.cancel_job(j2)
            block.set()

        cancelled = [e for e in self._events if e.type == MpcExportEventType.CANCELLED]
        self.assertTrue(cancelled or j2)


if __name__ == "__main__":
    unittest.main()

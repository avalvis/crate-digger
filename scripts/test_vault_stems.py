"""Focused tests for adding stem separation to existing Vault tracks."""

from __future__ import annotations

import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.database import TrackRecord, VaultDatabase
from core.stems import StemModel
from ui.tabs.vault import _StemSeparationDialog


class VaultStemSeparationTest(unittest.TestCase):
    def test_database_marks_stems_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = VaultDatabase(Path(tmp) / "vault.db")
            try:
                track_id = db.upsert_track(
                    TrackRecord(
                        file_path=str(Path(tmp) / "track.m4a"),
                        artist="Artist",
                        title="Title",
                    )
                )
                stems_path = str(Path(tmp) / "stems")

                db.set_track_stems(track_id, stems_path)

                updated = db.get_track(track_id)
                self.assertTrue(updated.stems_separated)
                self.assertEqual(updated.stems_path, stems_path)
            finally:
                db.close()

    def test_worker_separates_local_audio_and_updates_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "track.m4a"
            audio_path.touch()
            track = TrackRecord(
                id=7,
                file_path=str(audio_path),
                artist="Artist",
                title="Title",
            )

            dialog = _StemSeparationDialog.__new__(_StemSeparationDialog)
            dialog._tracks = [track]
            dialog._model = StemModel.HTDEMUCS_FT
            dialog._separator = MagicMock()
            dialog._database = MagicMock()
            dialog._cancel_event = threading.Event()
            dialog._log = logging.getLogger("test.vault-stems")
            dialog.after = lambda _delay, callback: callback()
            dialog._on_finished = MagicMock()

            dialog._run_worker()

            stems_dir = audio_path.parent / "stems"
            dialog._separator.separate.assert_called_once_with(
                audio_path,
                stems_dir,
                progress_callback=unittest.mock.ANY,
                cancel_event=dialog._cancel_event,
                model=StemModel.HTDEMUCS_FT,
            )
            dialog._database.set_track_stems.assert_called_once_with(
                7, str(stems_dir)
            )
            dialog._on_finished.assert_called_once_with(1, [], False)


if __name__ == "__main__":
    unittest.main()

"""End-to-end smoke test for the full engine: exporter + pipeline + queue."""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from core.analyzer import AudioAnalyzer
from core.artwork import ArtworkProcessor
from core.database import TrackFilter, VaultDatabase
from core.downloader import Downloader
from core.exporter import MPCExporter
from core.metadata import MetadataWriter
from core.pipeline import IngestionPipeline, PipelineRequest
from core.queue_manager import QueueEvent, QueueEventType, QueueManager
from core.stems import StemModel, StemSeparator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
TEST_URLS = [
    "https://www.youtube.com/watch?v=ApXoWvfEYVU",
    # Add a second short URL for concurrency tests if desired.
]


def build_engine(workdir: Path):
    vault = workdir / "vault"
    staging = workdir / "staging"
    db_path = workdir / "vault.db"
    vault.mkdir(); staging.mkdir()

    db = VaultDatabase(db_path)
    pipeline = IngestionPipeline(
        downloader=Downloader(ffmpeg_path=FFMPEG),
        artwork=ArtworkProcessor(),
        analyzer=AudioAnalyzer(),
        metadata_writer=MetadataWriter(),
        stem_separator=StemSeparator(model=StemModel.HTDEMUCS_FT),
        database=db,
        vault_root=vault,
        staging_root=staging,
    )
    qm = QueueManager(pipeline=pipeline, database=db, num_workers=2)
    return db, pipeline, qm, vault


def test_pipeline_direct() -> None:
    """Run the pipeline synchronously (no queue) to verify end-to-end."""
    print("\n=== PIPELINE (direct) ===")
    with tempfile.TemporaryDirectory() as td:
        db, pipeline, _, vault = build_engine(Path(td))

        req = PipelineRequest(
            source_url=TEST_URLS[0],
            enable_stems=False,
            hint_genre="Test",
        )
        events: list = []
        result = pipeline.run(
            req, progress_callback=events.append,
        )

        print(f"  stages seen: {len({e.stage for e in events})}")
        print(f"  final_audio_path = {result.final_audio_path}")
        print(f"  bpm={result.analysis.bpm}  key={result.analysis.musical_key}")
        assert result.final_audio_path.exists()
        assert str(vault) in str(result.final_audio_path)
        assert result.final_audio_path.suffix == ".m4a"

        tracks = db.list_tracks(TrackFilter())
        assert len(tracks) == 1
        assert tracks[0].genre == "Test"
        assert tracks[0].bpm is not None
        print(f"  db row: id={tracks[0].id} path={tracks[0].file_path}")

        # Verify vault path sanitization — the parent dir name should
        # NOT contain any forbidden characters.
        parent_name = result.final_audio_path.parent.name
        for forbidden in '<>:"/\\|?*':
            assert forbidden not in parent_name, \
                f"Forbidden char {forbidden!r} in vault dir: {parent_name}"
        print(f"  vault dir sanitized: {parent_name}")

        db.close()


def test_queue_manager() -> None:
    """Verify event stream + concurrent job handling."""
    print("\n=== QUEUE MANAGER ===")
    with tempfile.TemporaryDirectory() as td:
        db, _, qm, _ = build_engine(Path(td))

        event_log: list[QueueEvent] = []
        drain_event = threading.Event()

        def subscriber(e: QueueEvent) -> None:
            event_log.append(e)
            if e.type == QueueEventType.QUEUE_DRAINED:
                drain_event.set()

        qm.events.subscribe(subscriber)
        qm.start()
        try:
            jid = qm.enqueue(PipelineRequest(
                source_url=TEST_URLS[0],
                enable_stems=False,
                hint_genre="Test",
            ))
            print(f"  enqueued job_id={jid}")

            # Wait for completion via event stream, NOT DB polling
            assert drain_event.wait(timeout=300), \
                "QUEUE_DRAINED event never fired (job hung?)"

            # Verify the event sequence
            types_seen = [e.type for e in event_log]
            print(f"  events: {len(event_log)}")
            assert QueueEventType.JOB_ENQUEUED in types_seen
            assert QueueEventType.JOB_STARTED in types_seen
            assert QueueEventType.JOB_PROGRESS in types_seen
            assert QueueEventType.JOB_COMPLETED in types_seen
            assert QueueEventType.QUEUE_DRAINED in types_seen

            # Verify events carried enrichment as work progressed
            progress_events = [e for e in event_log
                               if e.type == QueueEventType.JOB_PROGRESS]
            assert any(e.bpm is not None for e in progress_events), \
                "No progress event ever carried BPM"
            assert any(e.display_name for e in progress_events), \
                "No progress event ever carried display_name"
            print(f"  progress events: {len(progress_events)}")

            completed = [e for e in event_log
                         if e.type == QueueEventType.JOB_COMPLETED][0]
            print(f"  final: track_id={completed.track_id}  "
                  f"bpm={completed.bpm}  key={completed.musical_key}")
            assert completed.track_id is not None
            assert Path(completed.final_path).exists()

        finally:
            qm.shutdown(timeout=5.0)
            db.close()
        print("  queue manager lifecycle clean.")


def test_queue_cancel() -> None:
    """Verify mid-job cancellation fires JOB_CANCELLED cleanly."""
    print("\n=== QUEUE CANCEL ===")
    with tempfile.TemporaryDirectory() as td:
        db, _, qm, _ = build_engine(Path(td))

        cancelled = threading.Event()

        def on_event(e: QueueEvent) -> None:
            if e.type == QueueEventType.JOB_CANCELLED:
                cancelled.set()

        qm.events.subscribe(on_event)
        qm.start()
        try:
            jid = qm.enqueue(PipelineRequest(
                source_url=TEST_URLS[0], enable_stems=False,
            ))
            # Wait for download to start, then cancel
            time.sleep(1.5)
            fired = qm.cancel(jid)
            print(f"  cancel({jid}) returned: {fired}")
            assert cancelled.wait(timeout=20), "JOB_CANCELLED never fired"

            job = [j for j in db.list_queue_jobs() if j.id == jid][0]
            assert job.status == "cancelled"
            print(f"  db status: {job.status}")
        finally:
            qm.shutdown(timeout=5.0)
            db.close()


def test_exporter() -> None:
    """Verify exporter after a real pipeline run."""
    print("\n=== EXPORTER ===")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        db, pipeline, _, vault = build_engine(td_path)

        # Ingest one track first so we have something to export
        result = pipeline.run(PipelineRequest(
            source_url=TEST_URLS[0], hint_genre="Test",
        ))
        sd_card = td_path / "SD_CARD"
        sd_card.mkdir()

        events: list = []
        exporter = MPCExporter(ffmpeg_path=FFMPEG)
        out = exporter.export_batch(
            [result.final_audio_path], sd_card,
            progress_callback=events.append,
        )

        assert len(out.exported) == 1
        assert len(out.failed) == 0
        exp = out.exported[0]
        print(f"  exported: {exp.destination_path}")
        print(f"  size: {exp.wav_size_bytes:,} bytes in {exp.elapsed_seconds:.1f}s")
        assert exp.destination_path.exists()
        assert exp.destination_path.suffix == ".wav"
        assert exp.wav_size_bytes > 1_000_000    # ~10MB/min for 16-bit/44.1

        # Verify WAV header claims 16-bit / 44100 Hz
        import wave
        with wave.open(str(exp.destination_path), "rb") as w:
            assert w.getframerate() == 44100
            assert w.getsampwidth() == 2        # 16-bit = 2 bytes/sample
            assert w.getnchannels() == 2
            print(f"  wav: {w.getframerate()} Hz, "
                  f"{w.getsampwidth() * 8}-bit, {w.getnchannels()} ch")

        # No .partial files left behind
        leftover = list(sd_card.glob("*.partial"))
        assert not leftover, f"partial files left behind: {leftover}"

        db.close()


def main() -> int:
    test_pipeline_direct()
    test_queue_manager()
    test_queue_cancel()
    test_exporter()
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
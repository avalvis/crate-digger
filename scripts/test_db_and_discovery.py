"""Manual smoke test for core/database.py + core/discovery.py."""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from core.database import (
    DiscoveryRecord, ExportRecord, QueueJobRecord, TrackFilter,
    TrackRecord, VaultDatabase,
)
from core.discovery import (
    DiscoveryEngine, DiscoveryError, DiscoveryFilters, NoResultsError,
)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")


def test_database() -> None:
    print("\n=== DATABASE ===")
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "vault.db"
        db = VaultDatabase(db_path)

        # Schema bootstrap verification
        ver = db.get_meta("schema_version")
        print(f"  schema_version = {ver}")
        assert ver == "1"

        # Insert
        tid = db.upsert_track(TrackRecord(
            file_path="/tmp/fake/song.m4a",
            artist="João Gilberto",
            title="Corcovado",
            genre="Jazz",
            bpm=92.5,
            musical_key="Am",
            camelot_key="8A",
            year=1964,
            source_url="https://music.youtube.com/watch?v=TEST",
            source_platform="youtube_music",
        ))
        print(f"  inserted track id={tid}")

        # Idempotent upsert
        tid2 = db.upsert_track(TrackRecord(
            file_path="/tmp/fake/song.m4a",
            artist="João Gilberto",
            title="Corcovado (remastered)",   # updated
            genre="Jazz",
            source_url="https://music.youtube.com/watch?v=TEST",
            source_platform="youtube_music",
        ))
        assert tid == tid2, "upsert should reuse id for same file_path"
        t = db.get_track(tid)
        assert t.title.endswith("(remastered)")
        assert t.decade == 1960
        print("  idempotent upsert verified (title updated, decade derived)")

        # FTS search
        results = db.list_tracks(TrackFilter(query="gilberto"))
        assert len(results) == 1
        print(f"  fts search 'gilberto' → {len(results)} hit")

        results = db.list_tracks(TrackFilter(query="corcov"))  # prefix
        assert len(results) == 1
        print(f"  fts prefix 'corcov' → {len(results)} hit")

        # Filter combinations
        r = db.list_tracks(TrackFilter(genre="Jazz", min_bpm=80, max_bpm=100))
        assert len(r) == 1
        r = db.list_tracks(TrackFilter(genre="Jazz", min_bpm=120))
        assert len(r) == 0
        print("  filters (genre + bpm range) verified")

        # Queue job lifecycle
        jid = db.create_queue_job(QueueJobRecord(
            source_url="https://music.youtube.com/watch?v=X",
            status="pending",
        ))
        db.update_queue_job(
            jid, status="downloading",
            started_at="2025-01-01T00:00:00+00:00", progress_pct=50.0,
        )
        stuck = db.reset_stuck_jobs()
        assert stuck == 1
        jobs = db.list_queue_jobs()
        assert jobs[0].status == "failed"
        print(f"  queue: reset {stuck} stuck job(s)")

        # Discovery dedup
        db.record_discovery(DiscoveryRecord(
            discogs_master_id=999, artist="X", title="Y",
        ))
        assert db.is_already_suggested(999)
        assert not db.is_already_suggested(1000)
        # Re-insert same master is a no-op
        db.record_discovery(DiscoveryRecord(
            discogs_master_id=999, artist="X", title="Y",
        ))
        print("  discovery dedup verified")

        # Export log
        eid = db.record_export(ExportRecord(
            track_id=tid, destination_path="/Volumes/MPC_SD/song.wav",
            destination_device="MPC_SD", wav_size_bytes=41_000_000,
        ))
        t_after = db.get_track(tid)
        assert t_after.export_count == 1
        assert t_after.last_exported_at is not None
        print(f"  export logged id={eid}, track.export_count=1")

        # Concurrent writers
        print("\n  [concurrency] 8 threads × 10 inserts each...")
        errors: list[Exception] = []

        def insert_batch(i: int) -> None:
            try:
                for j in range(10):
                    db.upsert_track(TrackRecord(
                        file_path=f"/tmp/c/{i}_{j}.m4a",
                        artist=f"Artist{i}", title=f"Title{j}",
                        source_url="u", source_platform="manual",
                    ))
            except Exception as e:
                errors.append(e)

        t0 = time.monotonic()
        threads = [threading.Thread(target=insert_batch, args=(i,))
                   for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        elapsed = time.monotonic() - t0
        total = db.count_tracks()
        assert not errors, f"errors under concurrency: {errors}"
        assert total == 81, f"expected 81 rows, got {total}"   # 80 + the first
        print(f"  [concurrency] OK: 81 rows in {elapsed:.2f}s, no errors")

        # Reconcile detects stale paths
        rec = db.reconcile()
        print(f"  reconcile: {rec}")
        assert rec["missing"] == rec["total"]  # all our test paths are fake

        db.close()


def test_discovery() -> None:
    print("\n=== DISCOVERY ===")
    if not DISCOGS_TOKEN:
        print("  Skipping — set DISCOGS_TOKEN env var to run this section.")
        return

    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        eng = DiscoveryEngine(db=db, discogs_token=DISCOGS_TOKEN)

        # Sanity: missing token error path (exercise update)
        eng.update_discogs_token(None)
        try:
            eng.dig(DiscoveryFilters(decade=1970, genre="Jazz"))
        except DiscoveryError as e:
            print(f"  no-token path raises correctly: {type(e).__name__}")
        eng.update_discogs_token(DISCOGS_TOKEN)

        # Real dig
        filters = DiscoveryFilters(
            decade=1970, country="Brazil", genre="Jazz", min_have=30,
        )
        print(f"  digging: {filters}")
        try:
            sug = eng.dig(filters)
        except NoResultsError:
            print("  no results for Brazil/Jazz/70s — try broader filters")
            return
        print(f"  → {sug.display_name}")
        print(f"    year={sug.year} country={sug.country} style={sug.style}")
        print(f"    yt={sug.youtube_url}")
        print(f"    score={sug.match_score:.2f}")

        assert sug.youtube_video_id
        assert sug.match_score > 0.5
        assert db.is_already_suggested(sug.discogs_master_id), \
            "dig() must record the discovery immediately"

        # Rate-limit resilience: fire 10 digs in quick succession.
        # With our limiter (55/min) this should complete cleanly.
        print("\n  [rate-limit] 10 rapid digs...")
        t0 = time.monotonic()
        successes = 0
        for i in range(10):
            try:
                eng.dig(DiscoveryFilters(
                    decade=1970 + (i % 3) * 10, genre="Jazz", min_have=20,
                ))
                successes += 1
            except DiscoveryError as e:
                print(f"    dig {i} raised: {type(e).__name__}: {e}")
        elapsed = time.monotonic() - t0
        stats = eng.get_stats()
        print(f"  [rate-limit] {successes}/10 succeeded in {elapsed:.1f}s")
        print(f"  [rate-limit] discogs reqs={stats.discogs_requests} "
              f"rate_waits={stats.discogs_rate_waits:.1f}s "
              f"throttle_events={stats.throttle_events}")
        assert successes >= 7, "rate-limiter should keep most digs working"

        db.close()


def main() -> int:
    test_database()
    test_discovery()
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
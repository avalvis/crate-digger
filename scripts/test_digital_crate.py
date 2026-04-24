"""Interactive smoke test for Digital Crate tab + final ui/app.py.

Run modes:
  Default: stubbed DiscoveryEngine; simulates realistic waits + errors.
  DISCOGS_TOKEN env set: real DiscoveryEngine (requires Discogs token).
"""
from __future__ import annotations

import logging
import os
import random
import tempfile
import threading
import time
from pathlib import Path

from core.database import VaultDatabase
from core.discovery import (
    DiscoveryFilters, DiscoverySuggestion, NoResultsError,
    NoYouTubeMatchError, DiscoveryThrottledError,
)
from core.pipeline import PipelineStage
from core.queue_manager import EventBus, QueueEvent, QueueEventType
from ui.app import CrateDiggerApp
from utils.config import ConfigManager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


# ─── Stub discovery engine ────────────────────────────────────────

class _StubDiscoveryEngine:
    """Simulates realistic dig latency and occasional failures."""

    _FIXTURE_SUGGESTIONS = [
        DiscoverySuggestion(
            discogs_master_id=1001,
            discogs_release_id=2001,
            artist="Pharoah Sanders",
            title="Karma",
            year=1969, country="USA",
            genre="Jazz", style="Spiritual Jazz",
            youtube_url="https://music.youtube.com/watch?v=fake_karma",
            youtube_video_id="fake_karma",
            youtube_title="Pharoah Sanders — The Creator Has a Master Plan",
            youtube_duration_seconds=32 * 60 + 45,
            match_score=0.94,
        ),
        DiscoverySuggestion(
            discogs_master_id=1002,
            discogs_release_id=2002,
            artist="Alice Coltrane",
            title="Journey in Satchidananda",
            year=1971, country="USA",
            genre="Jazz", style="Spiritual Jazz",
            youtube_url="https://music.youtube.com/watch?v=fake_journey",
            youtube_video_id="fake_journey",
            youtube_title="Journey in Satchidananda (album version)",
            youtube_duration_seconds=6 * 60 + 35,
            match_score=0.88,
        ),
        DiscoverySuggestion(
            discogs_master_id=1003,
            discogs_release_id=2003,
            artist="Milton Nascimento",
            title="Clube da Esquina",
            year=1972, country="Brazil",
            genre="Latin", style="MPB",
            youtube_url="https://music.youtube.com/watch?v=fake_clube",
            youtube_video_id="fake_clube",
            youtube_title="Clube da Esquina — Cais",
            youtube_duration_seconds=4 * 60 + 12,
            match_score=0.91,
        ),
    ]

    def __init__(self, config: ConfigManager):
        self._config = config
        self._counter = 0
        self._rng = random.Random(1337)

    def update_discogs_token(self, token):
        pass

    def dig(self, filters: DiscoveryFilters) -> DiscoverySuggestion:
        self._counter += 1
        # Simulate rate-limit wait on every 3rd call
        wait = self._rng.uniform(1.5, 3.5)
        if self._counter % 3 == 0:
            wait = self._rng.uniform(5.0, 8.0)
        time.sleep(wait)

        # Occasional failure modes for error-path testing
        if self._counter % 5 == 0:
            raise NoYouTubeMatchError(
                "Couldn't find a matching video on YouTube Music"
            )
        if self._counter == 7:
            raise DiscoveryThrottledError("Discogs rate-limited us")
        if self._counter == 11:
            raise NoResultsError("No masters matched those filters")

        return self._rng.choice(self._FIXTURE_SUGGESTIONS)


class _StubQueueManager:
    def __init__(self):
        self.events = EventBus()
        self._next_id = 2000

    def start(self): pass
    def shutdown(self, **_k): pass

    def enqueue(self, request):
        jid = self._next_id
        self._next_id += 1
        self.events.publish(QueueEvent(
            type=QueueEventType.JOB_ENQUEUED, job_id=jid,
            source_url=request.source_url,
        ))
        # Quick simulate progression
        def sim():
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_STARTED, job_id=jid,
                source_url=request.source_url,
                stage=PipelineStage.DOWNLOADING,
            ))
            for pct in range(0, 101, 5):
                time.sleep(0.05)
                self.events.publish(QueueEvent(
                    type=QueueEventType.JOB_PROGRESS, job_id=jid,
                    display_name="Queued from Digital Crate",
                    stage=PipelineStage.DOWNLOADING,
                    overall_percent=float(pct),
                ))
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_COMPLETED, job_id=jid,
                display_name="Queued from Digital Crate",
                track_id=jid, overall_percent=100.0,
            ))
            self.events.publish(QueueEvent(
                type=QueueEventType.QUEUE_DRAINED,
            ))
        threading.Thread(target=sim, daemon=True).start()
        return jid

    def cancel(self, job_id): return True


def _stub_other_tabs():
    """Vault stays a placeholder; Manual Rip + Settings are real."""
    import sys, types
    import customtkinter as ctk

    if "ui.tabs.vault" not in sys.modules:
        mod = types.ModuleType("ui.tabs.vault")

        class VaultTab(ctk.CTkFrame):
            def __init__(self, parent, ctx):
                super().__init__(parent, fg_color=ctx.theme.surface.app)
                ctk.CTkLabel(
                    self, text="Vault — next batch",
                    text_color=ctx.theme.text.muted,
                    font=ctx.theme.font.heading,
                ).place(relx=0.5, rely=0.5, anchor="center")

            def focus_track(self, _tid): pass

        mod.VaultTab = VaultTab
        sys.modules["ui.tabs.vault"] = mod


def main() -> int:
    _stub_other_tabs()

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        config = ConfigManager(td_path / "config.json")
        config.load()

        # Seed a fake token so the Digital Crate's token warning is hidden.
        config.set_discogs_token("fake-token-for-stub-engine")

        db = VaultDatabase(td_path / "vault.db")
        qm = _StubQueueManager()
        qm.start()

        real_mode = bool(os.environ.get("DISCOGS_TOKEN"))
        if real_mode:
            from core.discovery import DiscoveryEngine
            discovery = DiscoveryEngine(
                db=db, discogs_token=os.environ["DISCOGS_TOKEN"],
            )
            print("[real] Using actual DiscoveryEngine with your Discogs token.")
        else:
            discovery = _StubDiscoveryEngine(config)
            print("[stub] Using simulated DiscoveryEngine. Set DISCOGS_TOKEN "
                  "for real mode.")

        app = CrateDiggerApp(
            queue_manager=qm,            # type: ignore[arg-type]
            database=db,
            config=config,
            discovery=discovery,         # type: ignore[arg-type]
            exporter=None,
        )
        try:
            app.run()
        finally:
            qm.shutdown()
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
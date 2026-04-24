"""Interactive smoke test for Manual Rip + Settings tabs.

Boots the app shell with real config persistence and a stubbed
queue manager so you can interact with the form, toggle stems,
paste URLs, and see how the two tabs integrate.
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from core.database import VaultDatabase
from core.pipeline import PipelineStage
from core.queue_manager import EventBus, QueueEvent, QueueEventType
from ui.app import CrateDiggerApp
from utils.config import ConfigManager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


class _StubQueueManager:
    """Enough surface area to satisfy the app + Manual Rip tab."""

    def __init__(self):
        self.events = EventBus()
        self._next_id = 1000
        self._stopped = threading.Event()

    def start(self):
        pass

    def shutdown(self, timeout=5.0, cancel_in_flight=True):
        self._stopped.set()

    def enqueue(self, request):
        jid = self._next_id
        self._next_id += 1

        # Synthesize a realistic event sequence for this job.
        self.events.publish(QueueEvent(
            type=QueueEventType.JOB_ENQUEUED, job_id=jid,
            source_url=request.source_url,
        ))

        def simulate():
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_STARTED, job_id=jid,
                source_url=request.source_url,
                stage=PipelineStage.DOWNLOADING,
            ))
            stages = [
                (PipelineStage.DOWNLOADING, "Downloading audio stream", 10, 35),
                (PipelineStage.ANALYZING, "Detecting BPM + key", 35, 60),
                (PipelineStage.FETCHING_ARTWORK, "Fetching artwork", 60, 66),
                (PipelineStage.TAGGING, "Writing metadata", 66, 72),
                (PipelineStage.RELOCATING, "Filing in vault", 72, 78),
                (PipelineStage.INDEXING, "Indexing", 78, 82),
            ]
            if request.enable_stems:
                stages.append(
                    (PipelineStage.SEPARATING_STEMS, "Separating stems", 82, 99),
                )

            display_name = f"Test Artist — {request.source_url[-8:]}"
            for stage, message, lo, hi in stages:
                for pct in range(lo, hi + 1, 2):
                    if self._stopped.is_set():
                        return
                    time.sleep(0.08)
                    self.events.publish(QueueEvent(
                        type=QueueEventType.JOB_PROGRESS, job_id=jid,
                        source_url=request.source_url,
                        display_name=display_name,
                        stage=stage,
                        overall_percent=float(pct),
                        message=message,
                        bpm=94.5 if pct > 55 else None,
                        musical_key="Am" if pct > 58 else None,
                        camelot_key="8A" if pct > 58 else None,
                    ))

            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_COMPLETED, job_id=jid,
                source_url=request.source_url,
                display_name=display_name,
                track_id=jid,
                bpm=94.5, musical_key="Am", camelot_key="8A",
                final_path=f"/tmp/fake/{jid}.m4a",
                overall_percent=100.0,
            ))
            self.events.publish(QueueEvent(
                type=QueueEventType.QUEUE_DRAINED,
            ))

        threading.Thread(target=simulate, daemon=True).start()
        return jid

    def cancel(self, job_id):
        self.events.publish(QueueEvent(
            type=QueueEventType.JOB_CANCELLED, job_id=job_id,
            message="Cancelled",
        ))
        return True


def _stub_other_tabs():
    """Keep Digital Crate & Vault as 'Coming soon' placeholders."""
    import sys, types
    import customtkinter as ctk

    for mod_name, cls_name, label in [
        ("ui.tabs.digital_crate", "DigitalCrateTab", "Digital Crate"),
        ("ui.tabs.vault", "VaultTab", "Vault"),
    ]:
        if mod_name in sys.modules:
            continue
        mod = types.ModuleType(mod_name)

        def _make(lbl):
            class Placeholder(ctk.CTkFrame):
                def __init__(self, parent, ctx):
                    super().__init__(parent, fg_color=ctx.theme.surface.app)
                    ctk.CTkLabel(
                        self, text=f"{lbl} — coming next",
                        text_color=ctx.theme.text.muted,
                        font=ctx.theme.font.heading,
                    ).place(relx=0.5, rely=0.5, anchor="center")
            return Placeholder

        setattr(mod, cls_name, _make(label))
        sys.modules[mod_name] = mod


def main() -> int:
    _stub_other_tabs()

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        config_path = td_path / "config.json"
        db_path = td_path / "vault.db"

        config = ConfigManager(config_path)
        config.load()

        db = VaultDatabase(db_path)
        qm = _StubQueueManager()
        qm.start()

        # AppContext additions we didn't have in the earlier shell test:
        # config needs to be injected. Since ui/app.py needs to know
        # about config, temporarily monkey-patch AppContext to add it.
        from ui.app import AppContext
        # Monkey-patch the dataclass to include `config` (in real code
        # we'd update AppContext itself — doing that next file pass).
        AppContext.config = property(lambda self: config)  # type: ignore[attr-defined]

        app = CrateDiggerApp(
            queue_manager=qm,  # type: ignore[arg-type]
            database=db,
            discovery=None,
            exporter=None,
            vault_root_getter=lambda: config.snapshot().config.general.vault_root,
        )
        try:
            app.run()
        finally:
            qm.shutdown()
            db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
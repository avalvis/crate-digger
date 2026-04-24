"""Smoke test for ui/app.py + ui/events.py + ui/theme.py.

Boots the app with a stub queue manager that emits synthetic events
on a timer. Lets you click around the sidebar and watch toasts +
queue-status updates fire without needing any real pipeline runs.

Prerequisites: just CustomTkinter (no ffmpeg / yt-dlp / demucs needed).
"""
from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from core.database import VaultDatabase
from core.queue_manager import EventBus, QueueEvent, QueueEventType
from core.pipeline import PipelineStage
from ui.app import CrateDiggerApp
from utils.config import ConfigManager


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


class _StubQueueManager:
    """Looks enough like QueueManager for CrateDiggerApp to bind."""

    def __init__(self):
        self.events = EventBus()
        self._stopped = threading.Event()
        self._emitter_thread = None

    def start(self):
        self._emitter_thread = threading.Thread(
            target=self._emit_loop, daemon=True, name="stub-emitter",
        )
        self._emitter_thread.start()

    def shutdown(self, timeout=5.0, cancel_in_flight=True):
        self._stopped.set()

    def _emit_loop(self):
        """Emit a fake job every ~8 seconds so you can see the UI react."""
        job_id = 1000
        while not self._stopped.is_set():
            time.sleep(4.0)
            if self._stopped.is_set():
                break
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_ENQUEUED,
                job_id=job_id,
                source_url=f"https://fake/{job_id}",
                message="Enqueued",
            ))
            time.sleep(0.3)
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_STARTED,
                job_id=job_id,
                source_url=f"https://fake/{job_id}",
                stage=PipelineStage.DOWNLOADING,
                message="Started",
            ))
            # Emit a burst of progress events — verify coalescing
            for pct in range(0, 101, 3):
                if self._stopped.is_set():
                    break
                self.events.publish(QueueEvent(
                    type=QueueEventType.JOB_PROGRESS,
                    job_id=job_id,
                    source_url=f"https://fake/{job_id}",
                    display_name=f"Test Artist — Test Title #{job_id}",
                    stage=PipelineStage.DOWNLOADING,
                    overall_percent=float(pct),
                    stage_percent=float(pct),
                    message=f"Downloading {pct}%",
                    bpm=94.5 if pct > 40 else None,
                    musical_key="Am" if pct > 60 else None,
                    camelot_key="8A" if pct > 60 else None,
                ))
                time.sleep(0.05)  # deliberately faster than the 33ms pump
            self.events.publish(QueueEvent(
                type=QueueEventType.JOB_COMPLETED,
                job_id=job_id,
                source_url=f"https://fake/{job_id}",
                display_name=f"Test Artist — Test Title #{job_id}",
                track_id=job_id,
                bpm=94.5, musical_key="Am", camelot_key="8A",
                overall_percent=100.0,
                message="Complete",
            ))
            self.events.publish(QueueEvent(
                type=QueueEventType.QUEUE_DRAINED,
                message="Queue idle",
            ))
            job_id += 1


def _stub_tabs_if_missing():
    """
    The real tab modules aren't built yet; register placeholder
    classes that just show 'coming soon' frames so navigation works.
    """
    import sys, types
    import customtkinter as ctk

    for mod_name, cls_name, label in [
        ("ui.tabs.manual_rip",    "ManualRipTab",    "Manual Rip"),
        ("ui.tabs.digital_crate", "DigitalCrateTab", "Digital Crate"),
        ("ui.tabs.vault",         "VaultTab",        "Vault"),
        ("ui.tabs.settings",      "SettingsTab",     "Settings"),
    ]:
        if mod_name in sys.modules:
            continue

        module = types.ModuleType(mod_name)

        def _make_cls(lbl):
            class Placeholder(ctk.CTkFrame):
                def __init__(self, parent, ctx):
                    super().__init__(parent, fg_color=ctx.theme.surface.app)
                    card = ctk.CTkFrame(
                        self,
                        fg_color=ctx.theme.surface.raised,
                        border_color=ctx.theme.border.subtle,
                        border_width=1,
                        corner_radius=ctx.theme.radius.lg,
                    )
                    card.place(relx=0.5, rely=0.5, anchor="center",
                               relwidth=0.5, relheight=0.3)
                    ctk.CTkLabel(
                        card, text=f"{lbl}",
                        text_color=ctx.theme.text.primary,
                        font=ctx.theme.font.heading,
                    ).pack(pady=(30, 6))
                    ctk.CTkLabel(
                        card,
                        text="Coming in the next phase.",
                        text_color=ctx.theme.text.muted,
                        font=ctx.theme.font.body,
                    ).pack()
                    ctk.CTkButton(
                        card, text="Ping a test toast",
                        command=lambda: ctx.publish_toast(
                            f"Toast from {lbl}", kind="info",
                        ),
                    ).pack(pady=(20, 0))
            return Placeholder

        setattr(module, cls_name, _make_cls(label))
        sys.modules[mod_name] = module


def main() -> int:
    _stub_tabs_if_missing()

    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        qm = _StubQueueManager()
        qm.start()

        config = ConfigManager(Path(td) / "config.json")
        config.load()
        app = CrateDiggerApp(
            queue_manager=qm,  # type: ignore[arg-type]
            database=db,
            config=config,
            discovery=None,
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
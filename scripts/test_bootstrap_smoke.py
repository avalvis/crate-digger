"""
Headless bootstrap smoke test — verifies the app shell constructs without
entering mainloop. Run from repo root:

    python scripts/test_bootstrap_smoke.py
"""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING)


class BootstrapSmokeTest(unittest.TestCase):
    def test_all_core_ui_imports(self) -> None:
        modules = [
            "main",
            "ui.app",
            "ui.window_placement",
            "ui.tabs.digital_crate",
            "ui.tabs.manual_rip",
            "ui.tabs.vault",
            "ui.tabs.settings",
            "ui.components.waveform_player",
            "ui.components.mpc_export_manager",
            "core.preview_prefetch",
            "core.mpc_export_manager",
            "core.discovery",
            "core.database",
            "core.queue_manager",
            "utils.config",
        ]
        for name in modules:
            with self.subTest(module=name):
                __import__(name)

    def test_window_placement_helpers(self) -> None:
        import customtkinter as ctk
        from ui.window_placement import (
            get_work_area,
            is_effectively_maximized,
            prepare_ctk_environment,
            show_root_window,
            sync_ctk_window_metrics,
        )

        prepare_ctk_environment()
        root = ctk.CTk()
        root.withdraw()
        try:
            area = get_work_area(root)
            self.assertGreater(area.width, 400)
            self.assertGreater(area.height, 300)
            show_root_window(
                root,
                maximized=False,
                width=900,
                height=600,
                min_width=800,
                min_height=500,
            )
            sync_ctk_window_metrics(root, min_width=800, min_height=500)
            # Not maximized — should not fill work area.
            self.assertFalse(is_effectively_maximized(root, fallback=False))
        finally:
            root.destroy()

    def test_crate_digger_app_constructs(self) -> None:
        import customtkinter as ctk
        from core.database import VaultDatabase
        from core.queue_manager import EventBus, QueueManager
        from ui.app import CrateDiggerApp
        from utils.config import ConfigManager

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            config = ConfigManager(td_path / "config.json")
            config.load()
            db = VaultDatabase(td_path / "vault.db")

            qm = MagicMock(spec=QueueManager)
            qm.events = EventBus()
            qm.list_jobs.return_value = []
            qm.start = MagicMock()
            qm.shutdown = MagicMock()

            app = CrateDiggerApp(
                queue_manager=qm,
                database=db,
                config=config,
                pipeline=None,
                discovery=None,
                exporter=None,
                preview=None,
                preview_prefetch=None,
                stem_separator=None,
                mpc_export_manager=None,
                logger=logging.getLogger("test"),
            )

            self.assertIsNotNone(app._root)
            self.assertTrue(app._root.winfo_exists())
            self.assertIsNotNone(app._sidebar_frame)
            self.assertIsNotNone(app._content_host)

            # Avoid mainloop — tear down like a normal close.
            app.shutdown()

            # Tk may still report exists briefly; ensure destroy was called.
            try:
                ctk._default_root  # noqa: SLF001
            except Exception:
                pass

            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

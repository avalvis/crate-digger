"""
main.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Application Entry Point

Boots the app with the following sequence:
    1. Set up logging (rotating file handler to ~/.cratedigger/cratedigger.log).
    2. Load config (creates defaults on first run).
    3. Show splash window while provisioning ffmpeg.
    4. Open vault.db and run schema bootstrap.
    5. Construct the engine (downloader, analyzer, artwork, metadata,
       stems, exporter) with config-driven parameters.
    6. Construct IngestionPipeline and QueueManager; start workers.
    7. Construct DiscoveryEngine if a token is present.
    8. Close splash; construct CrateDiggerApp and enter mainloop.

Any failure in steps 1-7 surfaces as a splash error message rather
than a silent crash. Fatal errors log a traceback to cratedigger.log
before exiting.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

# Keep heavy imports deferred to after the splash shows, so the user
# sees visual feedback within ~100ms of launch.


# ─── Paths ──────────────────────────────────────────────────────────

APP_NAME = "CrateDigger"
APP_VERSION = "0.1.0"


def _patch_customtkinter_compat() -> None:
    """
    Backward-compat for CustomTkinter versions that reject width/height
    in `.place(...)` calls.

    The generated UI codebase uses many `.place(width=..., height=...)`
    placements. Newer CTk versions require those dimensions at widget
    construction time. This shim applies dimensions via `configure()`
    before delegating to CTk's original place.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return

    base = ctk.CTkBaseClass
    if getattr(base, "_cratedigger_place_patched", False):
        return

    original_place = base.place

    def _compat_place(self, **kwargs):
        width = kwargs.pop("width", None)
        height = kwargs.pop("height", None)
        if width is not None or height is not None:
            cfg = {}
            if width is not None:
                cfg["width"] = width
            if height is not None:
                cfg["height"] = height
            try:
                self.configure(**cfg)
            except Exception:
                pass
        return original_place(self, **kwargs)

    base.place = _compat_place
    base._cratedigger_place_patched = True


def _load_dotenv(project_root: Path) -> None:
    """
    Load a .env file from project_root into os.environ, without overwriting
    values already set by the real environment (same semantics as python-dotenv).
    Silently no-ops if the file is missing or unreadable.
    """
    env_file = project_root / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Strip surrounding quotes (both " and ') from the value.
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Don't clobber values already present in the real environment.
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass


def _app_data_dir() -> Path:
    """
    Platform-conventional per-user data directory for app state.
    Stores: config.json, cratedigger.log, vault.db (by default).
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        local = (
            Path(__import__("os").environ.get("LOCALAPPDATA", ""))
            or Path.home() / "AppData" / "Local"
        )
        return local / APP_NAME
    # Linux / other: follow XDG where possible.
    import os

    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


# ─── Logging ────────────────────────────────────────────────────────


def _setup_logging(data_dir: Path) -> logging.Logger:
    """
    Configure the root logger with rotating file + console handlers.
    Returns the app's named logger.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "cratedigger.log"

    # Rotating file handler: 5MB max, 3 backups (~15MB worst case).
    # More than enough for multi-day debugging on any consumer setup.
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)-28s  %(message)s",
    )
    file_handler.setFormatter(file_formatter)

    # Console handler: INFO and above. Suppressed in frozen builds
    # where stdout goes nowhere visible anyway.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter(
            "%(levelname)-7s  %(name)s  %(message)s",
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Clear any handlers set by libraries during their own import.
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Noisy libraries demoted to WARN — we still capture their DEBUG
    # via the file handler's higher level if we ever need them.
    for noisy in ("urllib3", "requests", "yt_dlp", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app_log = logging.getLogger("cratedigger")
    app_log.info("===== %s v%s starting =====", APP_NAME, APP_VERSION)
    app_log.info("Log file: %s", log_path)
    app_log.info("Platform: %s %s", sys.platform, sys.version.split()[0])
    return app_log


# ─── Splash window ──────────────────────────────────────────────────


class _SplashWindow:
    """
    Minimal splash. Shown during boot; closed once the main app is ready.
    Uses a very small subset of CTk so we're not paying for the full
    theme system during boot.
    """

    def __init__(self) -> None:
        import customtkinter as ctk

        try:
            if hasattr(ctk, "deactivate_automatic_dpi_awareness"):
                ctk.deactivate_automatic_dpi_awareness()
        except Exception:
            pass

        ctk.set_appearance_mode("dark")

        self._root = ctk.CTk()
        self._root.report_callback_exception = self._on_tk_callback_exception
        self._root.title(APP_NAME)
        self._root.overrideredirect(True)  # borderless
        self._root.configure(fg_color="#07080B")

        W, H = 420, 200
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = (screen_w - W) // 2
        y = (screen_h - H) // 2
        self._root.geometry(f"{W}x{H}+{x}+{y}")

        outer = ctk.CTkFrame(
            self._root,
            fg_color="#0F1116",
            border_color="#2B3140",
            border_width=1,
            corner_radius=12,
        )
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        brand_row = ctk.CTkFrame(outer, fg_color="transparent")
        brand_row.pack(anchor="w", padx=28, pady=(30, 8))

        mark = ctk.CTkFrame(
            brand_row,
            width=32,
            height=32,
            fg_color="#9D5CFF",
            corner_radius=6,
        )
        mark.pack(side="left", padx=(0, 12))
        mark.pack_propagate(False)

        stripe = ctk.CTkFrame(
            mark,
            fg_color="#2E7BFF",
            corner_radius=0,
            height=7,
        )
        stripe.place(relx=0.0, rely=0.62, relwidth=1.0)

        ctk.CTkLabel(
            brand_row,
            text=APP_NAME,
            text_color="#F5F7FA",
            font=("Helvetica Neue", 22, "bold"),
        ).pack(side="left")

        ctk.CTkLabel(
            outer,
            text="For the crate.  For the MPC.",
            text_color="#6B7280",
            font=("Helvetica Neue", 11),
        ).pack(anchor="w", padx=28, pady=(0, 22))

        self._status_label = ctk.CTkLabel(
            outer,
            text="Starting up…",
            text_color="#A8B0BD",
            font=("Helvetica Neue", 12),
        )
        self._status_label.pack(anchor="w", padx=28)

        # Indeterminate bar (CTk's progress bar in indeterminate mode
        # gives us a sweeping fill animation for free).
        self._progress = ctk.CTkProgressBar(
            outer,
            fg_color="#1D222C",
            progress_color="#2E7BFF",
            corner_radius=999,
            height=4,
            mode="indeterminate",
        )
        self._progress.pack(fill="x", padx=28, pady=(10, 28))
        self._progress.start()

        self._root.update_idletasks()
        self._root.update()

    def set_status(self, message: str) -> None:
        self._status_label.configure(text=message)
        self._root.update_idletasks()
        self._root.update()

    def show_error(self, message: str) -> None:
        """Show a persistent error state with a Close button."""
        self._progress.stop()
        self._progress.pack_forget()

        import customtkinter as ctk

        self._status_label.configure(
            text=message,
            text_color="#F05365",
            wraplength=360,
            justify="left",
        )

        ctk.CTkButton(
            self._root.children[list(self._root.children.keys())[0]],
            text="Close",
            command=self.close,
            fg_color="transparent",
            hover_color="#1D222C",
            text_color="#F5F7FA",
            border_color="#2B3140",
            border_width=1,
            corner_radius=8,
            width=90,
            height=32,
        ).pack(pady=(0, 20))

        self._root.update_idletasks()
        self._root.update()
        # Block until user dismisses so we don't destroy a window
        # they're still looking at.
        try:
            self._root.mainloop()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._progress.stop()
        except Exception:
            pass
        try:
            after_ids = self._root.tk.call("after", "info")
            if isinstance(after_ids, str):
                ids = [after_ids] if after_ids else []
            else:
                ids = list(after_ids)
            for aid in ids:
                try:
                    self._root.after_cancel(aid)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._root.destroy()
        except Exception:
            pass

    def _on_tk_callback_exception(self, exc, val, tb) -> None:
        """Suppress known harmless CTk teardown callback noise."""
        msg = str(val)
        if exc is RuntimeError and "invalid command name" in msg:
            ignored_tokens = (
                "check_dpi_scaling",
                "_set_scaled_min_max",
                "update",
            )
            if any(token in msg for token in ignored_tokens):
                return
        try:
            import traceback

            traceback.print_exception(exc, val, tb)
        except Exception:
            pass


# ─── Bootstrap ──────────────────────────────────────────────────────


def _bootstrap(splash: _SplashWindow, log: logging.Logger) -> int:
    """
    Construct every service the app needs. Returns a process exit code.
    Called from `main()` inside a try/except so failures surface on splash.
    """
    # Load .env from the project root before anything reads os.environ.
    _load_dotenv(Path(__file__).parent)

    # Deferred imports for snappy splash display
    splash.set_status("Loading configuration…")
    data_dir = _app_data_dir()

    from utils.config import ConfigManager

    config = ConfigManager(
        config_path=data_dir / "config.json",
        logger=log.getChild("config"),
    )
    snap = config.load()

    # ── ffmpeg ──
    splash.set_status("Checking ffmpeg…")
    from utils.ffmpeg_setup import FFmpegProvisioningError, provision_ffmpeg

    try:
        binaries = provision_ffmpeg(
            progress_callback=splash.set_status,
            logger=log.getChild("ffmpeg"),
        )
    except FFmpegProvisioningError as e:
        log.exception("ffmpeg provisioning failed")
        splash.show_error(
            f"Could not set up ffmpeg.\n\n{e}\n\nCheck cratedigger.log for details."
        )
        return 2

    log.info(
        "ffmpeg: %s (source=%s, version=%s)",
        binaries.ffmpeg_path,
        binaries.source,
        binaries.version,
    )

    # ── Database ──
    splash.set_status("Opening vault database…")
    from core.database import VaultDatabase

    db_path = data_dir / "vault.db"
    database = VaultDatabase(db_path, logger=log.getChild("db"))
    log.info("Database: %s", db_path)

    # ── Engine construction ──
    splash.set_status("Preparing engine…")

    from core.ai_metadata import make_ai_enricher
    from core.analyzer import AudioAnalyzer
    from core.artwork import ArtworkProcessor
    from core.downloader import Downloader
    from core.exporter import MPCExporter
    from core.metadata import MetadataWriter
    from core.pipeline import IngestionPipeline
    from core.queue_manager import QueueManager
    from core.stems import StemModel, StemSeparator

    downloader = Downloader(
        ffmpeg_path=binaries.ffmpeg_path,
        retries=snap.config.downloader.retries,
        fragment_retries=snap.config.downloader.fragment_retries,
        concurrent_fragments=snap.config.downloader.concurrent_fragments,
        logger=log.getChild("downloader"),
    )
    artwork = ArtworkProcessor(logger=log.getChild("artwork"))
    analyzer = AudioAnalyzer(
        ffmpeg_path=binaries.ffmpeg_path,
        logger=log.getChild("analyzer"),
    )
    metadata_writer = MetadataWriter(logger=log.getChild("metadata"))

    try:
        stem_model = StemModel(snap.config.stems.model)
    except ValueError:
        log.warning(
            "Unknown stem model in config: %r — using default.",
            snap.config.stems.model,
        )
        stem_model = StemModel.HTDEMUCS_FT

    stem_separator = StemSeparator(
        model=stem_model,
        device=snap.config.stems.device,
        ffmpeg_path=binaries.ffmpeg_path,
        logger=log.getChild("stems"),
    )

    splash.set_status("Validating stem runtime…")
    stems_ok, stems_info = stem_separator.probe_runtime()
    if stems_ok:
        log.info("Stem runtime ready: %s", stems_info)
    else:
        log.warning(
            "Stem runtime is unavailable. Stems jobs will fail until "
            "dependencies are fixed. Details: %s",
            stems_info,
        )

    exporter = MPCExporter(
        ffmpeg_path=binaries.ffmpeg_path,
        logger=log.getChild("exporter"),
    )

    vault_root = Path(snap.config.general.vault_root).expanduser()
    staging_root = Path(snap.config.general.staging_root).expanduser()

    # AI metadata enricher — opt-in via Settings; key stored in keyring or env.
    # Config-stored key (set via Settings UI) takes precedence over the env var.
    ai_enricher = None
    if snap.config.general.use_ai_metadata:
        deepseek_key = snap.deepseek_key or os.environ.get("DEEPSEEK_API_KEY", "")
        ai_enricher = make_ai_enricher(
            api_key=deepseek_key or None,
            logger=log.getChild("ai_metadata"),
        )
        if ai_enricher is None:
            log.info(
                "AI metadata enrichment enabled but no DeepSeek API key found "
                "(set one in Settings or DEEPSEEK_API_KEY env var)."
            )
        else:
            log.info("AI metadata enricher ready (DeepSeek).")

    pipeline = IngestionPipeline(
        downloader=downloader,
        artwork=artwork,
        analyzer=analyzer,
        metadata_writer=metadata_writer,
        stem_separator=stem_separator,
        database=database,
        vault_root=vault_root,
        staging_root=staging_root,
        ai_enricher=ai_enricher,
        folder_scheme=snap.config.general.vault_folder_scheme,
        logger=log.getChild("pipeline"),
    )

    queue_manager = QueueManager(
        pipeline=pipeline,
        database=database,
        num_workers=snap.config.general.concurrent_workers,
        logger=log.getChild("queue"),
    )
    queue_manager.start()

    # ── Discovery (optional; needs a token) ──
    discovery = None
    if snap.discogs_token:
        from core.discovery import DiscoveryEngine

        discovery = DiscoveryEngine(
            db=database,
            discogs_token=snap.discogs_token,
            logger=log.getChild("discovery"),
        )
        log.info("Discovery engine ready (token present).")
    else:
        log.info("No Discogs token configured; Discovery will be disabled.")

    # ── App ──
    splash.set_status("Opening Crate Digger…")
    from ui.app import CrateDiggerApp

    app = CrateDiggerApp(
        queue_manager=queue_manager,
        database=database,
        config=config,
        pipeline=pipeline,
        discovery=discovery,
        exporter=exporter,
        logger=log.getChild("app"),
    )

    # Close splash right before mainloop so the transition is visible.
    splash.close()

    try:
        app.run()
        return 0
    except Exception:
        log.exception("App run loop raised")
        return 1


# ─── Entry ──────────────────────────────────────────────────────────


def main() -> int:
    """Entry point. Never raises; returns an exit code."""
    try:
        _patch_customtkinter_compat()
        data_dir = _app_data_dir()
        log = _setup_logging(data_dir)
    except Exception:
        traceback.print_exc()
        return 3

    splash: Optional[_SplashWindow] = None
    try:
        splash = _SplashWindow()
    except Exception:
        log.exception("Splash construction failed")
        # If splash won't build, we can still try to boot headlessly
        # for developer environments — the app itself will raise its
        # own error and we'll get a traceback in the log.
        splash = None

    try:
        if splash is not None:
            return _bootstrap(splash, log)
        else:
            # Very unlikely path: proceed without a splash.
            return _bootstrap_headless(log)
    except Exception as e:
        log.exception("Fatal bootstrap error")
        if splash is not None:
            try:
                splash.show_error(
                    f"Crate Digger could not start.\n\n{e}\n\n"
                    f"See log at {data_dir / 'cratedigger.log'}."
                )
            except Exception:
                pass
        return 1


def _bootstrap_headless(log: logging.Logger) -> int:
    """Fallback when splash can't be constructed. Developer-only path."""

    class _NullSplash:
        def set_status(self, _msg: str) -> None:
            pass

        def close(self) -> None:
            pass

        def show_error(self, msg: str) -> None:
            log.error("Splash-error (headless): %s", msg)

    return _bootstrap(_NullSplash(), log)  # type: ignore[arg-type]


if __name__ == "__main__":
    sys.exit(main())

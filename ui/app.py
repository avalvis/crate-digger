"""
ui/app.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Application Root & Sidebar Shell

Top-level window and service bundle. Owns the lifecycle of the UI
event bridge, toast layer, and tab mounting. Services flow in via
the constructor and down to tabs via AppContext.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk
import tkinter as tk

from core.database import VaultDatabase
from core.queue_manager import QueueEvent, QueueEventType, QueueManager
from ui.components.sidebar import SidebarNavButton
from ui.components.toast import ToastKind, ToastLayer
from ui.events import UiEventBridge
from ui.theme import (
    Theme,
    apply_customtkinter_globals,
    build_theme,
    style_label_meta,
)
from utils.config import ConfigManager

if TYPE_CHECKING:
    from core.discovery import DiscoveryEngine
    from core.exporter import MPCExporter
    from core.pipeline import IngestionPipeline
    from core.preview import PreviewService


# ─── Nav configuration ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class NavItem:
    key: str
    label: str
    icon_glyph: str
    builder: Callable[["AppContext", ctk.CTkFrame], ctk.CTkFrame]


# ─── App context ────────────────────────────────────────────────────


@dataclass(slots=True)
class AppContext:
    theme: Theme
    event_bridge: UiEventBridge
    queue_manager: QueueManager
    database: VaultDatabase
    config: ConfigManager
    discovery: Optional["DiscoveryEngine"]
    exporter: Optional["MPCExporter"]
    logger: logging.Logger
    pipeline: Optional["IngestionPipeline"] = None  # for live enricher updates
    preview: Optional["PreviewService"] = None  # in-app waveform preview

    _toast_publisher: Optional[Callable[[str, ToastKind], None]] = None
    _tab_switcher: Optional[Callable[[str], None]] = None
    _vault_focuser: Optional[Callable[[int], None]] = None
    _config_listeners: list[Callable[[], None]] = field(
        default_factory=list, repr=False,
    )

    def on_config_changed(self, listener: Callable[[], None]) -> None:
        """Register a callback fired after credentials or prefs change."""
        if listener not in self._config_listeners:
            self._config_listeners.append(listener)

    def notify_config_changed(self) -> None:
        for cb in list(self._config_listeners):
            try:
                cb()
            except Exception:
                self.logger.exception("Config-change listener failed")

    def publish_toast(
        self,
        message: str,
        kind: ToastKind = "info",
        *,
        action_label: Optional[str] = None,
        action_callback: Optional[Callable[[], None]] = None,
        on_action_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        if self._toast_publisher is not None:
            self._toast_publisher(
                message,
                kind,
                action_label=action_label,  # type: ignore[call-arg]
                action_callback=action_callback,  # type: ignore[call-arg]
                on_action_error=on_action_error,  # type: ignore[call-arg]
            )

    def switch_to_tab(self, key: str) -> None:
        if self._tab_switcher is not None:
            self._tab_switcher(key)

    def focus_vault_track(self, track_id: int) -> None:
        if self._vault_focuser is not None:
            self._vault_focuser(track_id)


# ─── The App ────────────────────────────────────────────────────────


class CrateDiggerApp:
    APP_TITLE   = "Crate Digger"
    APP_VERSION = "0.1.0"

    def __init__(
        self,
        *,
        queue_manager: QueueManager,
        database: VaultDatabase,
        config: ConfigManager,
        pipeline: Optional["IngestionPipeline"] = None,
        discovery: Optional["DiscoveryEngine"] = None,
        exporter: Optional["MPCExporter"] = None,
        preview: Optional["PreviewService"] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._log = logger or logging.getLogger("cratedigger.app")
        self._queue = queue_manager
        self._db = database
        self._config = config
        self._pipeline = pipeline
        self._discovery = discovery
        self._exporter = exporter
        self._preview = preview

        self._root = ctk.CTk()
        self._root.report_callback_exception = self._on_tk_callback_exception
        try:
            tk._default_root = self._root  # type: ignore[attr-defined]
        except Exception:
            pass
        apply_customtkinter_globals(None)  # type: ignore[arg-type]
        self._theme = build_theme()
        self._configure_root_window()

        self._bridge = UiEventBridge(
            tk_root=self._root,
            core_bus=self._queue.events,
            logger=self._log.getChild("bridge"),
        )

        self._context = AppContext(
            theme=self._theme,
            event_bridge=self._bridge,
            queue_manager=self._queue,
            database=self._db,
            config=self._config,
            discovery=self._discovery,
            exporter=self._exporter,
            logger=self._log.getChild("tabs"),
            pipeline=self._pipeline,
            preview=self._preview,
        )
        self._context._toast_publisher = self._publish_toast
        self._context._tab_switcher = self._show_tab
        self._context._vault_focuser = self._switch_to_vault_and_focus

        self._nav_items: list[NavItem] = self._build_nav_items()
        self._nav_buttons: dict[str, SidebarNavButton] = {}
        self._built_tabs: dict[str, ctk.CTkFrame] = {}
        self._active_tab_key: Optional[str] = None

        self._sidebar_frame: Optional[ctk.CTkFrame] = None
        self._content_host: Optional[ctk.CTkFrame] = None
        self._queue_status_label: Optional[ctk.CTkLabel] = None
        self._toast_layer: Optional[ToastLayer] = None

        self._build_layout()

        self._bridge.subscribe(self._on_queue_event, weak=False)

        self._root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._bind_shortcuts()

    def _on_tk_callback_exception(self, exc, val, tb) -> None:
        msg = str(val)
        if exc is RuntimeError and "invalid command name" in msg:
            ignored_tokens = ("check_dpi_scaling", "_set_scaled_min_max", "update")
            if any(token in msg for token in ignored_tokens):
                self._log.debug("Suppressed Tk teardown noise: %s", msg)
                return
        self._log.exception("Unhandled Tk callback error", exc_info=(exc, val, tb))

    # ── Lifecycle ──

    def run(self) -> None:
        self._bridge.start()
        snap = self._config.snapshot()
        initial_tab = snap.config.ui.last_active_tab
        valid_keys = {n.key for n in self._nav_items}
        if initial_tab not in valid_keys:
            initial_tab = "manual_rip"
        self._root.after(0, lambda: self._show_tab(initial_tab))
        self._log.info("%s v%s ready.", self.APP_TITLE, self.APP_VERSION)
        self._root.mainloop()

    def shutdown(self) -> None:
        self._log.info("Shutting down…")
        try:
            self._persist_window_state()
        except Exception:
            self._log.exception("Could not persist window state")
        try:
            if self._toast_layer is not None:
                self._toast_layer.clear()
        except Exception:
            pass
        try:
            self._bridge.stop()
        except Exception:
            self._log.exception("Error stopping UI event bridge")
        try:
            self._queue.shutdown(timeout=5.0, cancel_in_flight=True)
        except Exception:
            self._log.exception("Error shutting down queue manager")
        try:
            self._db.close()
        except Exception:
            self._log.exception("Error closing database")
        try:
            self._cancel_pending_tk_after_callbacks()
        except Exception:
            self._log.exception("Error canceling pending Tk callbacks")
        try:
            self._root.destroy()
        except Exception:
            pass

    def _on_close_request(self) -> None:
        self.shutdown()

    def _persist_window_state(self) -> None:
        try:
            w = self._root.winfo_width()
            h = self._root.winfo_height()
        except Exception:
            return
        if w < 200 or h < 200:
            return
        updates: dict = {"window_width": int(w), "window_height": int(h)}
        if self._active_tab_key:
            updates["last_active_tab"] = self._active_tab_key
        try:
            self._config.update_ui(**updates)
        except Exception:
            self._log.exception("update_ui during shutdown failed")

    def _cancel_pending_tk_after_callbacks(self) -> None:
        try:
            after_ids = self._root.tk.call("after", "info")
        except Exception:
            return
        ids = [after_ids] if isinstance(after_ids, str) and after_ids else list(after_ids or [])
        for aid in ids:
            try:
                self._root.after_cancel(aid)
            except Exception:
                pass

    # ── Root window ──

    def _configure_root_window(self) -> None:
        t = self._theme
        r = self._root
        r.title(self.APP_TITLE)
        r.configure(fg_color=t.surface.app)
        r.minsize(t.window_min_width, t.window_min_height)
        snap = self._config.snapshot()
        init_w = max(t.window_min_width, snap.config.ui.window_width)
        init_h = max(t.window_min_height, snap.config.ui.window_height)
        screen_w = r.winfo_screenwidth()
        screen_h = r.winfo_screenheight()
        x = max(0, (screen_w - init_w) // 2)
        y = max(0, (screen_h - init_h) // 2)
        r.geometry(f"{init_w}x{init_h}+{x}+{y}")

    # ── Nav items ──

    def _build_nav_items(self) -> list[NavItem]:
        def _manual_rip(ctx: AppContext, host: ctk.CTkFrame) -> ctk.CTkFrame:
            from ui.tabs.manual_rip import ManualRipTab
            return ManualRipTab(host, ctx)

        def _digital_crate(ctx: AppContext, host: ctk.CTkFrame) -> ctk.CTkFrame:
            from ui.tabs.digital_crate import DigitalCrateTab
            return DigitalCrateTab(host, ctx)

        def _vault(ctx: AppContext, host: ctk.CTkFrame) -> ctk.CTkFrame:
            from ui.tabs.vault import VaultTab
            return VaultTab(host, ctx)

        def _settings(ctx: AppContext, host: ctk.CTkFrame) -> ctk.CTkFrame:
            from ui.tabs.settings import SettingsTab
            return SettingsTab(host, ctx)

        return [
            NavItem("manual_rip",    "Manual Rip",    "↓", _manual_rip),
            NavItem("digital_crate", "Digital Crate", "◉", _digital_crate),
            NavItem("vault",         "Vault",         "≡", _vault),
            NavItem("settings",      "Settings",      "⚙", _settings),
        ]

    # ── Layout ──

    def _build_layout(self) -> None:
        # col 0 = sidebar, col 1 = 1px separator, col 2 = content
        self._root.grid_columnconfigure(0, weight=0)
        self._root.grid_columnconfigure(1, weight=0)
        self._root.grid_columnconfigure(2, weight=1)
        self._root.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_separator()
        self._build_content_host()
        self._build_toast_layer()

    def _build_sidebar(self) -> None:
        t = self._theme

        sb = ctk.CTkFrame(
            self._root,
            width=t.sidebar_width,
            fg_color=t.surface.base,
            corner_radius=0,
            border_width=0,
        )
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_columnconfigure(0, weight=1)
        # row 3 spacer pushes footer to bottom
        sb.grid_rowconfigure(3, weight=1)

        # ── Brand ──
        brand = ctk.CTkFrame(sb, fg_color="transparent")
        brand.grid(
            row=0, column=0, sticky="ew",
            padx=t.space.lg,
            pady=(t.space.xl, t.space.md),
        )

        logo_row = ctk.CTkFrame(brand, fg_color="transparent")
        logo_row.pack(fill="x")

        # Vinyl disc logo
        disc = _build_vinyl_canvas(logo_row, t, size=38)
        disc.pack(side="left", padx=(0, t.space.md))

        brand_text = ctk.CTkFrame(logo_row, fg_color="transparent")
        brand_text.pack(side="left", anchor="w")

        ctk.CTkLabel(
            brand_text,
            text=self.APP_TITLE,
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            brand_text,
            text="For the crate.  For the MPC.",
            text_color=t.text.muted,
            font=t.font.micro,
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        # ── Divider ──
        _hairline(sb, t).grid(
            row=1, column=0, sticky="ew",
            padx=t.space.lg, pady=(0, t.space.sm),
        )

        # ── Nav list ──
        nav_list = ctk.CTkFrame(sb, fg_color="transparent")
        nav_list.grid(row=2, column=0, sticky="ew", padx=t.space.sm)

        for item in self._nav_items:
            btn = SidebarNavButton(
                nav_list,
                t,
                label=item.label,
                icon_glyph=item.icon_glyph,
                command=lambda k=item.key: self._show_tab(k),
            )
            btn.pack(fill="x", pady=(0, t.space.xxs))
            self._nav_buttons[item.key] = btn

        # ── Footer ──
        _hairline(sb, t).grid(
            row=4, column=0, sticky="ew",
            padx=t.space.lg, pady=(t.space.md, t.space.sm),
        )
        footer = ctk.CTkFrame(sb, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew",
                    padx=t.space.lg, pady=(0, t.space.lg))

        self._queue_status_label = ctk.CTkLabel(
            footer,
            text="Queue idle",
            **style_label_meta(t),
            anchor="w",
        )
        self._queue_status_label.pack(fill="x")

        ctk.CTkLabel(
            footer,
            text=f"v{self.APP_VERSION}",
            text_color=t.text.disabled,
            font=t.font.micro,
            anchor="w",
        ).pack(fill="x", pady=(t.space.xxs, 0))

        self._sidebar_frame = sb

    def _build_separator(self) -> None:
        t = self._theme
        sep = ctk.CTkFrame(
            self._root,
            width=1,
            fg_color=t.border.subtle,
            corner_radius=0,
            border_width=0,
        )
        sep.grid(row=0, column=1, sticky="ns")
        sep.grid_propagate(False)

    def _build_content_host(self) -> None:
        t = self._theme
        host = ctk.CTkFrame(
            self._root,
            fg_color=t.surface.app,
            corner_radius=0,
            border_width=0,
        )
        host.grid(row=0, column=2, sticky="nsew")
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        self._content_host = host

    def _build_toast_layer(self) -> None:
        assert self._content_host is not None
        self._toast_layer = ToastLayer(self._content_host, self._theme)

    # ── Keyboard shortcuts ──

    def _bind_shortcuts(self) -> None:
        mod = "Command" if sys.platform == "darwin" else "Control"
        for i, item in enumerate(self._nav_items[:9], start=1):
            self._root.bind_all(
                f"<{mod}-Key-{i}>",
                lambda _e, k=item.key: self._show_tab(k),
            )
        self._root.bind_all(
            f"<{mod}-comma>",
            lambda _e: self._show_tab("settings"),
        )
        self._root.bind_all(
            f"<{mod}-w>",
            lambda _e: self._on_close_request(),
        )

    # ── Tab switching ──

    def _show_tab(self, key: str) -> None:
        if key == self._active_tab_key:
            tab = self._built_tabs.get(key)
            if tab is not None and hasattr(tab, "on_tab_visible"):
                try:
                    tab.on_tab_visible()  # type: ignore[attr-defined]
                except Exception:
                    self._log.exception("Tab refresh failed for %s", key)
            return

        if key not in self._built_tabs:
            assert self._content_host is not None
            item = next((n for n in self._nav_items if n.key == key), None)
            if item is None:
                self._log.warning("Unknown nav key: %s", key)
                return
            try:
                frame = item.builder(self._context, self._content_host)
            except Exception as e:
                self._log.exception("Failed to build tab %s", key)
                self._publish_toast(f"Could not open {item.label}: {e}", "error")
                return
            frame.grid(row=0, column=0, sticky="nsew")
            self._built_tabs[key] = frame

        if (
            self._active_tab_key is not None
            and self._active_tab_key in self._built_tabs
        ):
            prev = self._built_tabs[self._active_tab_key]
            if hasattr(prev, "on_tab_hidden"):
                try:
                    prev.on_tab_hidden()  # type: ignore[attr-defined]
                except Exception:
                    pass
            prev.grid_remove()

        shown = self._built_tabs[key]
        shown.grid(row=0, column=0, sticky="nsew")
        self._active_tab_key = key
        self._update_nav_highlights()
        if hasattr(shown, "on_tab_visible"):
            try:
                shown.on_tab_visible()  # type: ignore[attr-defined]
            except Exception:
                self._log.exception("Tab visible hook failed for %s", key)
        if self._toast_layer is not None:
            try:
                self._toast_layer.lift()
            except Exception:
                pass

    def _update_nav_highlights(self) -> None:
        for key, btn in self._nav_buttons.items():
            btn.set_active(key == self._active_tab_key)

    # ── Queue event → sidebar ──

    def _on_queue_event(self, event: QueueEvent) -> None:
        label = self._queue_status_label
        if label is None:
            return
        t = self._theme

        if event.type == QueueEventType.QUEUE_DRAINED:
            label.configure(text="Queue idle", text_color=t.text.secondary)
        elif event.type == QueueEventType.JOB_STARTED:
            label.configure(text="Processing…", text_color=t.accent.purple)
        elif event.type == QueueEventType.JOB_PROGRESS:
            pct = int(event.overall_percent)
            name = event.display_name or "track"
            name_short = (name[:22] + "…") if len(name) > 23 else name
            label.configure(
                text=f"{pct}%  {name_short}",
                text_color=t.accent.purple,
            )
        elif event.type == QueueEventType.JOB_COMPLETED:
            track_id = event.track_id
            # Manual Rip shows inline success on the progress row — skip
            # the duplicate shell toast when that tab is active.
            if self._active_tab_key != "manual_rip":
                self._publish_toast(
                    f"Added: {event.display_name or 'track'}",
                    "success",
                    action_label="Show in Vault" if track_id else None,
                    action_callback=(
                        (lambda tid=track_id: self._switch_to_vault_and_focus(tid))
                        if track_id else None
                    ),
                    on_action_error=lambda _e: self._publish_toast(
                        "Could not open that track in the Vault.",
                        "warning",
                    ),
                )
        elif event.type == QueueEventType.JOB_FAILED:
            self._publish_toast(
                f"Failed: {event.error_message or 'unknown error'}",
                "error",
            )
        elif event.type == QueueEventType.JOB_CANCELLED:
            self._publish_toast(
                f"Cancelled: {event.display_name or 'track'}",
                "info",
            )

    def _switch_to_vault_and_focus(self, track_id: int) -> None:
        self._show_tab("vault")
        vault_tab = self._built_tabs.get("vault")
        if vault_tab is not None and hasattr(vault_tab, "focus_track"):
            try:
                vault_tab.focus_track(track_id)  # type: ignore[attr-defined]
            except Exception:
                self._log.exception("Could not focus track in Vault tab")

    # ── Toasts ──

    def _publish_toast(
        self,
        message: str,
        kind: ToastKind = "info",
        *,
        action_label: Optional[str] = None,
        action_callback: Optional[Callable[[], None]] = None,
        on_action_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        if self._toast_layer is None:
            return
        self._toast_layer.lift()
        self._toast_layer.show(
            message,
            kind=kind,
            action_label=action_label,
            action_callback=action_callback,
            on_action_error=on_action_error,
        )


# ─── Helpers ────────────────────────────────────────────────────────


def _hairline(parent, theme: Theme) -> ctk.CTkFrame:
    return ctk.CTkFrame(
        parent,
        height=1,
        fg_color=theme.border.subtle,
        corner_radius=0,
        border_width=0,
    )


def _build_vinyl_canvas(parent, theme: Theme, *, size: int = 38) -> tk.Canvas:
    """
    Draw a miniature vinyl record using a tk.Canvas. Produces a warm-toned
    disc with groove rings and an amber center label — the app's identity mark.
    """
    t = theme
    cx = cy = size // 2

    canvas = tk.Canvas(
        parent,
        width=size,
        height=size,
        bg=t.surface.base,
        highlightthickness=0,
        bd=0,
    )

    # Outer vinyl body — very dark warm black
    canvas.create_oval(
        1, 1, size - 1, size - 1,
        fill="#191410",
        outline=t.border.strong,
        width=1,
    )

    # Groove rings — subtle concentric lines at decreasing radii
    for shrink in (6, 10, 14):
        r = cx - shrink
        if r > 8:
            canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r,
                fill="",
                outline="#231D13",
                width=1,
            )

    # Center label — amber/ochre, like a 1970s vinyl press label
    lr = 9
    canvas.create_oval(
        cx - lr, cy - lr, cx + lr, cy + lr,
        fill="#7A5518",
        outline="#9A6D28",
        width=1,
    )

    # Spindle hole
    canvas.create_oval(
        cx - 2, cy - 2, cx + 2, cy + 2,
        fill=t.surface.app,
        outline="",
    )

    return canvas

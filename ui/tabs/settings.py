"""
ui/tabs/settings.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Settings Tab

Persistent user preferences surfaced as a form. Reads from and writes
to ConfigManager; field changes save immediately (debounced for text
inputs) so there's no "Save" button and no unsaved-state bookkeeping.

Sections:
  • Library        — vault root, staging root
  • Ingestion      — concurrent workers, default stems toggle
  • Stem Separation — model dropdown (HTDEMUCS_FT default), device
  • Discovery      — Discogs token with keyring-availability status
  • About          — app version, log location, reset defaults

All write paths validate through the config manager's pydantic schema;
invalid values produce a red-bordered field + toast rather than a save.
"""
from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional, TYPE_CHECKING

import customtkinter as ctk

from core.stems import StemModel
from ui.components.glow_entry import GlowEntry
from ui.theme import (
    style_card,
    style_card_elevated,
    style_danger_button,
    style_ghost_button,
    style_input,
    style_label_body,
    style_label_heading,
    style_label_meta,
    style_label_subheading,
    style_primary_button,
    style_secondary_button,
)
from utils.config import ConfigError

if TYPE_CHECKING:
    from ui.app import AppContext


# Display names for stem models — keeps the UI human-readable while
# the underlying StemModel enum stays stable.
_STEM_MODEL_CHOICES: list[tuple[StemModel, str]] = [
    (StemModel.HTDEMUCS_FT,  "htdemucs_ft  —  Fine-tuned, highest quality"),
    (StemModel.HTDEMUCS,     "htdemucs  —  Demucs v4 default"),
    (StemModel.HTDEMUCS_6S,  "htdemucs_6s  —  6 stems (adds piano + guitar)"),
    (StemModel.MDX_EXTRA,    "mdx_extra  —  ~2x faster on CPU"),
    (StemModel.MDX_EXTRA_Q,  "mdx_extra_q  —  Quantized, smallest memory"),
]

_DEVICE_CHOICES: list[tuple[str, str]] = [
    ("auto", "Auto  —  use best available"),
    ("cpu",  "CPU"),
    ("mps",  "Apple Silicon (MPS)"),
    ("cuda", "NVIDIA (CUDA)"),
]


class SettingsTab(ctk.CTkFrame):
    """Form-driven preferences editor. Saves on change."""

    _DEBOUNCE_MS = 500    # text field debounce before saving

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent, fg_color=ctx.theme.surface.app)

        self._ctx = ctx
        self._theme = ctx.theme
        self._config = ctx.config
        self._log = ctx.logger.getChild("settings")

        # Debounce timers per field key (so rapid typing doesn't thrash
        # the config file with 40 saves/second).
        self._debounce_timers: dict[str, str] = {}

        # Field widgets we need to read/write later.
        self._vault_entry: Optional[GlowEntry] = None
        self._staging_entry: Optional[GlowEntry] = None
        self._token_entry: Optional[GlowEntry] = None
        self._workers_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._stems_default_switch: Optional[ctk.CTkSwitch] = None
        self._stems_default_var: Optional[ctk.BooleanVar] = None
        self._ai_metadata_switch: Optional[ctk.CTkSwitch] = None
        self._ai_metadata_var: Optional[ctk.BooleanVar] = None
        self._stem_model_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._device_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._token_status_label: Optional[ctk.CTkLabel] = None
        self._keyring_warning_label: Optional[ctk.CTkLabel] = None

        self._build_body()
        self._populate_from_config()

    # ── Body construction ──

    def _build_body(self) -> None:
        t = self._theme

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=t.surface.app,
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=t.border.strong,
            scrollbar_button_hover_color=t.accent.blue,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        content = ctk.CTkFrame(scroll, fg_color="transparent")
        content.grid(row=0, column=0, sticky="ew",
                     padx=t.space.xl, pady=(t.space.xxl, t.space.xl))
        content.grid_columnconfigure(0, weight=1)
        self._content = content
        content.bind("<Configure>", self._on_content_configure)

        # Heading
        ctk.CTkLabel(
            content, text="Settings",
            **style_label_heading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            content,
            text="Preferences are saved automatically as you change them.",
            **style_label_meta(t),
        ).grid(row=1, column=0, sticky="w",
               pady=(t.space.xs, t.space.xl))

        # Section builders — each returns the next available row index.
        next_row = 2
        next_row = self._build_library_section(content, next_row)
        next_row = self._build_ingestion_section(content, next_row)
        next_row = self._build_stems_section(content, next_row)
        next_row = self._build_discovery_section(content, next_row)
        next_row = self._build_about_section(content, next_row)

    def _section_card(self, parent, row: int, title: str, subtitle: str = "") -> ctk.CTkFrame:
        """Render a section heading + card wrapper; return the card body frame."""
        t = self._theme

        ctk.CTkLabel(
            parent, text=title,
            **style_label_subheading(t),
        ).grid(row=row, column=0, sticky="w",
               pady=(0, t.space.xs))

        if subtitle:
            ctk.CTkLabel(
                parent, text=subtitle,
                **style_label_meta(t),
                wraplength=720, justify="left",
            ).grid(row=row + 1, column=0, sticky="w",
                   pady=(0, t.space.md))
            card_row = row + 2
        else:
            card_row = row + 1

        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=card_row, column=0, sticky="ew",
                  pady=(0, t.space.xl))
        card.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew",
                  padx=t.space.xl, pady=t.space.xl)
        body.grid_columnconfigure(0, weight=1)
        return body

    # ── Library section ──

    def _build_library_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent, row, "Library",
            "Where Crate Digger keeps your tracks on disk.",
        )

        # Vault root field + browse button
        self._field_label(body, 0, "Vault root",
                          "Final destination for every ingested track.")

        vault_row = ctk.CTkFrame(body, fg_color="transparent")
        vault_row.grid(row=1, column=0, sticky="ew", pady=(0, t.space.lg))
        vault_row.grid_columnconfigure(0, weight=1)

        self._vault_entry = GlowEntry(
            vault_row, t,
            placeholder="~/Music/CrateDigger_Vault",
            show_clear_button=False,
            on_submit=lambda v: self._save_vault_root(v),
        )
        self._vault_entry.grid(row=0, column=0, sticky="ew",
                               padx=(0, t.space.sm))
        # Also debounce-save on every keystroke so the user doesn't
        # have to hit Enter to persist.
        self._vault_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce(
                "vault_root", lambda: self._save_vault_root(self._vault_entry.get()),
            ),
            add="+",
        )

        ctk.CTkButton(
            vault_row, text="Browse…",
            command=self._pick_vault_root,
            **style_secondary_button(t),
            width=100,
        ).grid(row=0, column=1, sticky="e")

        # Staging root (secondary)
        self._field_label(body, 2, "Staging directory",
                          "Scratch space for in-progress downloads. "
                          "Can live on a fast SSD separate from the vault.")

        staging_row = ctk.CTkFrame(body, fg_color="transparent")
        staging_row.grid(row=3, column=0, sticky="ew")
        staging_row.grid_columnconfigure(0, weight=1)

        self._staging_entry = GlowEntry(
            staging_row, t,
            placeholder="~/.cratedigger/staging",
            show_clear_button=False,
            on_submit=lambda v: self._save_staging_root(v),
        )
        self._staging_entry.grid(row=0, column=0, sticky="ew",
                                 padx=(0, t.space.sm))
        self._staging_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce(
                "staging_root", lambda: self._save_staging_root(self._staging_entry.get()),
            ),
            add="+",
        )

        ctk.CTkButton(
            staging_row, text="Browse…",
            command=self._pick_staging_root,
            **style_secondary_button(t),
            width=100,
        ).grid(row=0, column=1, sticky="e")

        return row + 3   # heading + subtitle + card row

    # ── Ingestion section ──

    def _build_ingestion_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent, row, "Ingestion",
            "How Crate Digger processes queued jobs.",
        )

        # Concurrent workers dropdown
        self._field_label(body, 0, "Concurrent workers",
                          "Number of pipeline jobs running in parallel. "
                          "More = faster queues; also more CPU and network.")

        _workers_wrapper = ctk.CTkFrame(
            body,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        _workers_wrapper.grid(row=1, column=0, sticky="w", pady=(0, t.space.lg))
        self._workers_dropdown = ctk.CTkOptionMenu(
            _workers_wrapper,
            values=[str(n) for n in range(1, 9)],
            command=self._save_worker_count,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=156, height=38,
        )
        self._workers_dropdown.pack(padx=2, pady=2)

        # Default stems toggle
        self._field_label(
            body, 2, "Default 'Split stems' to on",
            "When enabled, the Manual Rip and Digital Crate tabs start "
            "with stem separation turned on.",
        )

        toggle_row = ctk.CTkFrame(body, fg_color="transparent")
        toggle_row.grid(row=3, column=0, sticky="w")

        self._stems_default_var = ctk.BooleanVar(value=False)
        self._stems_default_switch = ctk.CTkSwitch(
            toggle_row, text="",
            variable=self._stems_default_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_stems_default(self._stems_default_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=40, height=22,
        )
        self._stems_default_switch.pack()

        # AI metadata toggle
        self._field_label(
            body, 4, "AI metadata enrichment",
            "Use DeepSeek AI to extract the original artist name and title from "
            "YouTube video titles. Requires DEEPSEEK_API_KEY in your .env file. "
            "Disable if you prefer the raw upload metadata or have no API key.",
        )

        ai_toggle_row = ctk.CTkFrame(body, fg_color="transparent")
        ai_toggle_row.grid(row=5, column=0, sticky="w",
                           pady=(0, t.space.xs))

        self._ai_metadata_var = ctk.BooleanVar(value=True)
        self._ai_metadata_switch = ctk.CTkSwitch(
            ai_toggle_row, text="",
            variable=self._ai_metadata_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_ai_metadata(self._ai_metadata_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=40, height=22,
        )
        self._ai_metadata_switch.pack()

        return row + 3

    # ── Stem separation section ──

    def _build_stems_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent, row, "Stem separation",
            "Demucs configuration. Higher-quality models take longer to run.",
        )

        # Model dropdown
        self._field_label(body, 0, "Model")

        _stem_model_wrapper = ctk.CTkFrame(
            body,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        _stem_model_wrapper.grid(row=1, column=0, sticky="w", pady=(0, t.space.lg))
        self._stem_model_dropdown = ctk.CTkOptionMenu(
            _stem_model_wrapper,
            values=[label for _model, label in _STEM_MODEL_CHOICES],
            command=self._save_stem_model,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=436, height=38,
        )
        self._stem_model_dropdown.pack(padx=2, pady=2)

        # Device dropdown
        self._field_label(body, 2, "Compute device",
                          "'Auto' picks the best available. "
                          "Apple Silicon users: MPS is significantly faster than CPU.")

        _device_wrapper = ctk.CTkFrame(
            body,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        _device_wrapper.grid(row=3, column=0, sticky="w")
        self._device_dropdown = ctk.CTkOptionMenu(
            _device_wrapper,
            values=[label for _key, label in _DEVICE_CHOICES],
            command=self._save_device,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=256, height=38,
        )
        self._device_dropdown.pack(padx=2, pady=2)

        return row + 3

    # ── Discovery section ──

    def _build_discovery_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent, row, "Discovery",
            "Discogs API token is required for the Digital Crate 'Dig' feature.",
        )

        # Token field
        self._field_label(body, 0, "Discogs personal access token")

        token_row = ctk.CTkFrame(body, fg_color="transparent")
        token_row.grid(row=1, column=0, sticky="ew",
                       pady=(0, t.space.sm))
        token_row.grid_columnconfigure(0, weight=1)

        self._token_entry = GlowEntry(
            token_row, t,
            placeholder="Paste token (stored securely in OS keyring)",
            show_clear_button=True,
        )
        self._token_entry.grid(row=0, column=0, sticky="ew",
                               padx=(0, t.space.sm))

        # Token entry saves on focus-out rather than keystroke — safer
        # for secrets (no partial tokens written during typing).
        self._token_entry._entry.bind(
            "<FocusOut>",
            lambda _e: self._save_discogs_token(self._token_entry.get()),
            add="+",
        )
        self._token_entry._entry.bind(
            "<Return>",
            lambda _e: self._save_discogs_token(self._token_entry.get()),
            add="+",
        )

        ctk.CTkButton(
            token_row, text="Get token",
            command=lambda: webbrowser.open(
                "https://www.discogs.com/settings/developers",
            ),
            **style_ghost_button(t),
        ).grid(row=0, column=1, sticky="e")

        # Status label — shows 'stored in keyring' / 'not set' / 'plaintext fallback'
        self._token_status_label = ctk.CTkLabel(
            body, text="",
            text_color=t.text.muted,
            font=t.font.caption, anchor="w",
        )
        self._token_status_label.grid(row=2, column=0, sticky="w",
                                      pady=(0, t.space.xs))

        # Keyring-availability warning (shown only when unavailable)
        self._keyring_warning_label = ctk.CTkLabel(
            body,
            text=(
                "⚠  OS keyring is unavailable on this system. "
                "The token will be stored in a plaintext sibling file "
                "restricted to your user account."
            ),
            text_color=t.status.warning,
            font=t.font.caption,
            anchor="w", justify="left",
            wraplength=680,
        )
        self._keyring_warning_label.grid(row=3, column=0, sticky="w")
        self._keyring_warning_label.grid_remove()

        return row + 3

    # ── About section ──

    def _build_about_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(parent, row, "About")

        # Row 0: app version
        ver_row = ctk.CTkFrame(body, fg_color="transparent")
        ver_row.grid(row=0, column=0, sticky="ew", pady=(0, t.space.md))
        ver_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            ver_row, text="Version",
            text_color=t.text.secondary,
            font=t.font.caption,
            width=140, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            ver_row, text="0.1.0",
            text_color=t.text.primary,
            font=t.font.mono_body, anchor="w",
        ).grid(row=0, column=1, sticky="w")

        # Row 1: log path
        log_row = ctk.CTkFrame(body, fg_color="transparent")
        log_row.grid(row=1, column=0, sticky="ew", pady=(0, t.space.md))
        log_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            log_row, text="Log file",
            text_color=t.text.secondary,
            font=t.font.caption,
            width=140, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        log_path = str(Path.home() / ".cratedigger" / "cratedigger.log")
        ctk.CTkLabel(
            log_row, text=log_path,
            text_color=t.text.primary,
            font=t.font.mono_body, anchor="w",
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkButton(
            log_row, text="Open log",
            command=lambda: self._open_path(log_path),
            **style_ghost_button(t),
        ).grid(row=0, column=2, sticky="e")

        # Row 2: reset defaults
        reset_row = ctk.CTkFrame(body, fg_color="transparent")
        reset_row.grid(row=2, column=0, sticky="ew", pady=(t.space.md, 0))

        ctk.CTkButton(
            reset_row, text="Reset all settings to defaults",
            command=self._confirm_reset,
            **style_danger_button(t),
        ).pack(anchor="w")

        return row + 1

    # ── Initial population from config ──

    def _populate_from_config(self) -> None:
        snap = self._config.snapshot()
        cfg = snap.config

        if self._vault_entry is not None:
            self._vault_entry.set(cfg.general.vault_root)

        if self._staging_entry is not None:
            self._staging_entry.set(cfg.general.staging_root)

        if self._workers_dropdown is not None:
            self._workers_dropdown.set(str(cfg.general.concurrent_workers))

        if self._stems_default_var is not None:
            self._stems_default_var.set(cfg.general.enable_stems_by_default)

        if self._ai_metadata_var is not None:
            self._ai_metadata_var.set(cfg.general.use_ai_metadata)

        if self._stem_model_dropdown is not None:
            label = self._label_for_stem_model(cfg.stems.model)
            self._stem_model_dropdown.set(label)

        if self._device_dropdown is not None:
            label = self._label_for_device(cfg.stems.device)
            self._device_dropdown.set(label)

        if self._token_entry is not None and snap.discogs_token:
            # Show a masked representation so the user knows something
            # is stored, but never the actual token in case of screenshots.
            self._token_entry.set("•" * 20)

        # Keyring warning visibility
        if self._keyring_warning_label is not None:
            if snap.keyring_available:
                self._keyring_warning_label.grid_remove()
            else:
                self._keyring_warning_label.grid()

        # Token status text
        self._refresh_token_status(snap)

    # ── Save handlers ──

    def _save_vault_root(self, value: str) -> None:
        value = self._normalize_path(value)
        if not value:
            return
        try:
            # Best-effort mkdir so the directory exists after the user saves.
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._ctx.publish_toast(
                f"Could not create folder: {e}", kind="error",
            )
            return

        try:
            self._config.update_general(vault_root=str(Path(value).expanduser()))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_staging_root(self, value: str) -> None:
        value = self._normalize_path(value)
        if not value:
            return
        try:
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._ctx.publish_toast(
                f"Could not create folder: {e}", kind="error",
            )
            return
        try:
            self._config.update_general(staging_root=str(Path(value).expanduser()))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_worker_count(self, value: str) -> None:
        try:
            n = int(value)
        except ValueError:
            return
        try:
            self._config.update_general(concurrent_workers=n)
            self._ctx.publish_toast(
                f"Worker count set to {n}. Takes effect on next app launch.",
                kind="info",
            )
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_stems_default(self, value: bool) -> None:
        try:
            self._config.update_general(enable_stems_by_default=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_ai_metadata(self, value: bool) -> None:
        try:
            self._config.update_general(use_ai_metadata=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_stem_model(self, label: str) -> None:
        model = self._stem_model_from_label(label)
        if model is None:
            return
        try:
            self._config.update_stems(model=model.value)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_device(self, label: str) -> None:
        key = self._device_key_from_label(label)
        if key is None:
            return
        try:
            self._config.update_stems(device=key)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_discogs_token(self, value: str) -> None:
        value = (value or "").strip()
        # If the field currently shows the masked value we rendered on
        # load, don't treat that as a real token.
        if value and set(value) == {"•"}:
            return

        try:
            snap = self._config.set_discogs_token(value or None)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
            return

        # Notify the live discovery engine so the user doesn't have to
        # restart the app to use the new token.
        if self._ctx.discovery is not None:
            try:
                self._ctx.discovery.update_discogs_token(snap.discogs_token)
            except Exception:
                self._log.exception("Failed to propagate token to DiscoveryEngine")

        # Reflect the save in UI
        if value:
            self._token_entry.set("•" * 20)
            self._ctx.publish_toast(
                "Discogs token saved.", kind="success",
            )
        else:
            self._token_entry.set("")
            self._ctx.publish_toast(
                "Discogs token cleared.", kind="info",
            )
        self._refresh_token_status(snap)

    # ── Browse buttons ──

    def _pick_vault_root(self) -> None:
        current = self._vault_entry.get() if self._vault_entry else ""
        initial = str(Path(current).expanduser()) if current else str(Path.home())
        chosen = filedialog.askdirectory(
            title="Choose Vault root",
            initialdir=initial,
            mustexist=False,
        )
        if chosen:
            self._vault_entry.set(chosen)
            self._save_vault_root(chosen)

    def _pick_staging_root(self) -> None:
        current = self._staging_entry.get() if self._staging_entry else ""
        initial = str(Path(current).expanduser()) if current else str(Path.home())
        chosen = filedialog.askdirectory(
            title="Choose staging directory",
            initialdir=initial,
            mustexist=False,
        )
        if chosen:
            self._staging_entry.set(chosen)
            self._save_staging_root(chosen)

    # ── Reset ──

    def _confirm_reset(self) -> None:
        # CTk ships a messagebox but it looks out of place. A minimal
        # confirm dialog built from theme primitives matches our look.
        _ResetConfirmDialog(
            parent=self,
            theme=self._theme,
            on_confirm=self._perform_reset,
        )

    def _perform_reset(self) -> None:
        try:
            # Reset every section by writing empty dicts (which triggers
            # pydantic to apply defaults, since missing fields fall back).
            self._config.update_general(
                vault_root=str(Path.home() / "Music" / "CrateDigger_Vault"),
                staging_root=str(Path.home() / ".cratedigger" / "staging"),
                concurrent_workers=2,
                enable_stems_by_default=False,
                use_ai_metadata=True,
            )
            self._config.update_stems(model="htdemucs_ft", device="auto")
            self._config.update_downloader(
                retries=5, fragment_retries=5, concurrent_fragments=4,
            )
            # Don't clear the Discogs token on reset — user would lose
            # it and have to re-enter. Reset is for *preferences*, not
            # credentials.
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
            return

        self._populate_from_config()
        self._ctx.publish_toast(
            "Settings reset to defaults.", kind="success",
        )

    # ── Token status text ──

    def _refresh_token_status(self, snap) -> None:
        t = self._theme
        if self._token_status_label is None:
            return

        if snap.discogs_token:
            if snap.keyring_available:
                self._token_status_label.configure(
                    text="✓  Token stored securely in OS keyring.",
                    text_color=t.status.success,
                )
            else:
                self._token_status_label.configure(
                    text="⚠  Token stored in plaintext fallback "
                         "(keyring unavailable).",
                    text_color=t.status.warning,
                )
        else:
            self._token_status_label.configure(
                text="No token set. Digital Crate discovery will be disabled.",
                text_color=t.text.muted,
            )

    # ── Helpers ──

    def _field_label(
        self, parent, row: int, title: str, description: str = "",
    ) -> None:
        """Render a form-field title + optional description block."""
        t = self._theme

        ctk.CTkLabel(
            parent, text=title,
            **style_label_body(t),
            anchor="w",
        ).grid(row=row, column=0, sticky="w",
               pady=(t.space.xs, 0))

        if description:
            ctk.CTkLabel(
                parent, text=description,
                **style_label_meta(t),
                wraplength=680, justify="left",
                anchor="w",
            ).grid(row=row, column=0, sticky="w",
                   pady=(18, t.space.xs))
            # The description shares the row; the preceding title gets
            # pinned to top via pady. For a cleaner layout we could move
            # title/desc into their own subframe, but this is adequate.

    def _debounce(self, key: str, fn: Callable[[], None]) -> None:
        """Run `fn` after DEBOUNCE_MS of quiet on the given key."""
        existing = self._debounce_timers.get(key)
        if existing is not None:
            try:
                self.after_cancel(existing)
            except Exception:
                pass
        self._debounce_timers[key] = self.after(self._DEBOUNCE_MS, fn)

    @staticmethod
    def _normalize_path(value: str) -> str:
        return (value or "").strip().rstrip("/\\")

    @staticmethod
    def _label_for_stem_model(model_value: str) -> str:
        for model, label in _STEM_MODEL_CHOICES:
            if model.value == model_value:
                return label
        return _STEM_MODEL_CHOICES[0][1]

    @staticmethod
    def _stem_model_from_label(label: str) -> Optional[StemModel]:
        for model, model_label in _STEM_MODEL_CHOICES:
            if model_label == label:
                return model
        return None

    @staticmethod
    def _label_for_device(device_key: str) -> str:
        for key, label in _DEVICE_CHOICES:
            if key == device_key:
                return label
        return _DEVICE_CHOICES[0][1]

    @staticmethod
    def _device_key_from_label(label: str) -> Optional[str]:
        for key, device_label in _DEVICE_CHOICES:
            if device_label == label:
                return key
        return None

    def _open_path(self, path: str) -> None:
        try:
            webbrowser.open(Path(path).expanduser().as_uri())
        except Exception as e:
            self._log.warning("Could not open %s: %s", path, e)

    def _on_content_configure(self, _event) -> None:
        t = self._theme
        parent_width = self._content.master.winfo_width()
        target = min(960, max(480, parent_width - 2 * t.space.xl))
        current_width = self._content.winfo_width()
        if abs(current_width - target) > 8:
            self._content.configure(width=target)


# ─── Confirm dialog for destructive actions ──────────────────────────

class _ResetConfirmDialog(ctk.CTkToplevel):
    """Themed, modal confirmation for Reset."""

    def __init__(self, parent, theme, on_confirm: Callable[[], None]) -> None:
        super().__init__(parent)

        self._theme = theme
        self._on_confirm = on_confirm

        t = theme
        self.title("Reset settings")
        self.configure(fg_color=t.surface.base)
        self.geometry("440x200")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent.winfo_toplevel())

        # Center over parent
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(f"+{px + (pw - 440) // 2}+{py + (ph - 200) // 2}")

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True,
                   padx=t.space.xl, pady=t.space.xl)

        ctk.CTkLabel(
            frame,
            text="Reset all settings to defaults?",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            frame,
            text="Your Discogs token and Vault contents will be preserved.",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w", justify="left",
            wraplength=380,
        ).pack(anchor="w", pady=(t.space.sm, t.space.xl))

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x")

        ctk.CTkButton(
            buttons, text="Cancel",
            command=self._cancel,
            **style_secondary_button(t),
        ).pack(side="right", padx=(t.space.sm, 0))

        ctk.CTkButton(
            buttons, text="Reset",
            command=self._confirm,
            **style_danger_button(t),
        ).pack(side="right")

        self.bind("<Escape>", lambda _e: self._cancel())

    def _confirm(self) -> None:
        try:
            self._on_confirm()
        finally:
            self.destroy()

    def _cancel(self) -> None:
        self.destroy()
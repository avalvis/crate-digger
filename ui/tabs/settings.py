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

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from core.stems import StemModel
from ui.components.glow_entry import GlowEntry
from utils.paths import VAULT_FOLDER_SCHEMES
from ui.theme import (
    style_card_elevated,
    style_danger_button,
    style_ghost_button,
    style_input,
    style_label_body,
    style_label_heading,
    style_label_meta,
    style_label_subheading,
    style_secondary_button,
)
from utils.config import ConfigError

if TYPE_CHECKING:
    from ui.app import AppContext


# Display names for stem models — keeps the UI human-readable while
# the underlying StemModel enum stays stable.
_STEM_MODEL_CHOICES: list[tuple[StemModel, str]] = [
    (StemModel.HTDEMUCS_FT, "htdemucs_ft  —  Fine-tuned, highest quality"),
    (StemModel.HTDEMUCS, "htdemucs  —  Demucs v4 default"),
    (StemModel.HTDEMUCS_6S, "htdemucs_6s  —  6 stems (adds piano + guitar)"),
    (StemModel.MDX_EXTRA, "mdx_extra  —  ~2x faster on CPU"),
    (StemModel.MDX_EXTRA_Q, "mdx_extra_q  —  Quantized, smallest memory"),
]

_DEVICE_CHOICES: list[tuple[str, str]] = [
    ("auto", "Auto  —  use best available"),
    ("cpu", "CPU"),
    ("mps", "Apple Silicon (MPS)"),
    ("cuda", "NVIDIA (CUDA)"),
]


class SettingsTab(ctk.CTkFrame):
    """Form-driven preferences editor. Saves on change."""

    _DEBOUNCE_MS = 500  # text field debounce before saving

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
        self._mpc_root_entry: Optional[GlowEntry] = None
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
        self._folder_scheme_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._deepseek_entry: Optional[GlowEntry] = None
        self._deepseek_status_label: Optional[ctk.CTkLabel] = None
        self._min_have_entry: Optional[ctk.CTkEntry] = None
        self._max_have_entry: Optional[ctk.CTkEntry] = None
        self._reel_size_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._prioritize_switch: Optional[ctk.CTkSwitch] = None
        self._prioritize_var: Optional[ctk.BooleanVar] = None
        self._intensity_slider: Optional[ctk.CTkSlider] = None
        self._intensity_label: Optional[ctk.CTkLabel] = None
        self._compilations_switch: Optional[ctk.CTkSwitch] = None
        self._compilations_var: Optional[ctk.BooleanVar] = None
        self._preview_volume_slider: Optional[ctk.CTkSlider] = None
        self._preview_volume_label: Optional[ctk.CTkLabel] = None
        self._prefetch_enabled_var: Optional[ctk.BooleanVar] = None
        self._prefetch_enabled_switch: Optional[ctk.CTkSwitch] = None
        self._prefetch_concurrency_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._prefetch_keep_decoded_var: Optional[ctk.BooleanVar] = None
        self._prefetch_keep_decoded_switch: Optional[ctk.CTkSwitch] = None
        self._mpc_export_workers_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._export_sr_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._export_bits_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._discovery_health_label: Optional[ctk.CTkLabel] = None

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
        content.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=t.space.xl,
            pady=(t.space.xxl, t.space.xl),
        )
        content.grid_columnconfigure(0, weight=1)
        self._content = content
        content.bind("<Configure>", self._on_content_configure)

        # Heading
        ctk.CTkLabel(
            content,
            text="Settings",
            **style_label_heading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            content,
            text="Preferences are saved automatically as you change them.",
            **style_label_meta(t),
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.xl))

        # Section builders — each returns the next available row index.
        next_row = 2
        next_row = self._build_library_section(content, next_row)
        next_row = self._build_ingestion_section(content, next_row)
        next_row = self._build_stems_section(content, next_row)
        next_row = self._build_discovery_behavior_section(content, next_row)
        next_row = self._build_export_section(content, next_row)
        next_row = self._build_discovery_section(content, next_row)
        next_row = self._build_about_section(content, next_row)

    def _section_card(
        self, parent, row: int, title: str, subtitle: str = ""
    ) -> ctk.CTkFrame:
        """Render a section heading + card wrapper; return the card body frame."""
        t = self._theme

        ctk.CTkLabel(
            parent,
            text=title,
            **style_label_subheading(t),
        ).grid(row=row, column=0, sticky="w", pady=(0, t.space.xs))

        if subtitle:
            ctk.CTkLabel(
                parent,
                text=subtitle,
                **style_label_meta(t),
                wraplength=720,
                justify="left",
            ).grid(row=row + 1, column=0, sticky="w", pady=(0, t.space.md))
            card_row = row + 2
        else:
            card_row = row + 1

        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=card_row, column=0, sticky="ew", pady=(0, t.space.xl))
        card.grid_columnconfigure(0, weight=1)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        body.grid_columnconfigure(0, weight=1)
        return body

    # ── Library section ──

    def _build_library_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent,
            row,
            "Library",
            "Where Crate Digger keeps your tracks on disk.",
        )

        # Vault root field + browse button
        self._field_label(
            body, 0, "Vault root", "Final destination for every ingested track."
        )

        vault_row = ctk.CTkFrame(body, fg_color="transparent")
        vault_row.grid(row=1, column=0, sticky="ew", pady=(0, t.space.lg))
        vault_row.grid_columnconfigure(0, weight=1)

        self._vault_entry = GlowEntry(
            vault_row,
            t,
            placeholder="~/Music/CrateDigger_Vault",
            show_clear_button=False,
            on_submit=lambda v: self._save_vault_root(v),
        )
        self._vault_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))
        # Also debounce-save on every keystroke so the user doesn't
        # have to hit Enter to persist.
        self._vault_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce(
                "vault_root",
                lambda: self._save_vault_root(self._vault_entry.get()),
            ),
            add="+",
        )

        ctk.CTkButton(
            vault_row,
            text="Open",
            command=self._open_vault_root,
            **style_ghost_button(t),
            width=72,
        ).grid(row=0, column=1, sticky="e", padx=(0, t.space.xs))

        ctk.CTkButton(
            vault_row,
            text="Browse…",
            command=self._pick_vault_root,
            **style_secondary_button(t),
            width=100,
        ).grid(row=0, column=2, sticky="e")

        # Staging root (secondary)
        self._field_label(
            body,
            2,
            "Staging directory",
            "Scratch space for in-progress downloads. "
            "Can live on a fast SSD separate from the vault.",
        )

        staging_row = ctk.CTkFrame(body, fg_color="transparent")
        staging_row.grid(row=3, column=0, sticky="ew")
        staging_row.grid_columnconfigure(0, weight=1)

        self._staging_entry = GlowEntry(
            staging_row,
            t,
            placeholder="~/.cratedigger/staging",
            show_clear_button=False,
            on_submit=lambda v: self._save_staging_root(v),
        )
        self._staging_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))
        self._staging_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce(
                "staging_root",
                lambda: self._save_staging_root(self._staging_entry.get()),
            ),
            add="+",
        )

        ctk.CTkButton(
            staging_row,
            text="Browse…",
            command=self._pick_staging_root,
            **style_secondary_button(t),
            width=100,
        ).grid(row=0, column=1, sticky="e")

        # Vault folder naming scheme
        self._field_label(
            body,
            4,
            "Vault folder naming",
            "Controls how new tracks are organized on disk. "
            "Existing tracks are not moved when you change this.",
        )

        _scheme_wrapper = ctk.CTkFrame(
            body,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        _scheme_wrapper.grid(row=5, column=0, sticky="w")
        self._folder_scheme_dropdown = ctk.CTkOptionMenu(
            _scheme_wrapper,
            values=[label for _key, label in VAULT_FOLDER_SCHEMES],
            command=self._save_folder_scheme,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=480,
            height=38,
        )
        self._folder_scheme_dropdown.pack(padx=2, pady=2)

        # MPC Samples folder (destination for the Digital Crate "MPC
        # Workflow" button — typically an MPC SD card path).
        self._field_label(
            body,
            6,
            "MPC Samples folder",
            "Destination for the Digital Crate → MPC Workflow button. "
            "Stems are organized as <folder>/<Artist - Title>/<stem>.wav "
            "and never touch the Vault.",
        )

        mpc_row = ctk.CTkFrame(body, fg_color="transparent")
        mpc_row.grid(row=7, column=0, sticky="ew", pady=(t.space.lg, 0))
        mpc_row.grid_columnconfigure(0, weight=1)

        self._mpc_root_entry = GlowEntry(
            mpc_row,
            t,
            placeholder="E:\\Samples\\Crate Digger",
            show_clear_button=False,
            on_submit=lambda v: self._save_mpc_root(v),
        )
        self._mpc_root_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))
        self._mpc_root_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce(
                "mpc_samples_root",
                lambda: self._save_mpc_root(self._mpc_root_entry.get()),
            ),
            add="+",
        )

        ctk.CTkButton(
            mpc_row,
            text="Open",
            command=self._open_mpc_root,
            **style_ghost_button(t),
            width=72,
        ).grid(row=0, column=1, sticky="e", padx=(0, t.space.xs))

        ctk.CTkButton(
            mpc_row,
            text="Browse…",
            command=self._pick_mpc_root,
            **style_secondary_button(t),
            width=100,
        ).grid(row=0, column=2, sticky="e")

        return row + 3  # heading + subtitle + card row

    # ── Ingestion section ──

    def _build_ingestion_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent,
            row,
            "Ingestion",
            "How Crate Digger processes queued jobs.",
        )

        # Concurrent workers dropdown
        self._field_label(
            body,
            0,
            "Concurrent workers",
            "Number of pipeline jobs running in parallel. "
            "More = faster queues; also more CPU and network.",
        )

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
            width=156,
            height=38,
        )
        self._workers_dropdown.pack(padx=2, pady=2)

        self._field_label(
            body,
            2,
            "MPC export workers",
            "Simultaneous MPC exports from Digital Crate. Keep at 1 unless "
            "you have a fast CPU — demucs is heavy.",
        )
        _mpc_workers_wrapper = ctk.CTkFrame(
            body,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        _mpc_workers_wrapper.grid(row=3, column=0, sticky="w", pady=(0, t.space.lg))
        self._mpc_export_workers_dropdown = ctk.CTkOptionMenu(
            _mpc_workers_wrapper,
            values=[str(n) for n in range(1, 5)],
            command=self._save_mpc_export_workers,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=156,
            height=38,
        )
        self._mpc_export_workers_dropdown.pack(padx=2, pady=2)

        # Default stems toggle
        self._field_label(
            body,
            4,
            "Default 'Split stems' to on",
            "When enabled, the Manual Rip and Digital Crate tabs start "
            "with stem separation turned on.",
        )

        toggle_row = ctk.CTkFrame(body, fg_color="transparent")
        toggle_row.grid(row=5, column=0, sticky="w")

        self._stems_default_var = ctk.BooleanVar(value=False)
        self._stems_default_switch = ctk.CTkSwitch(
            toggle_row,
            text="",
            variable=self._stems_default_var,
            onvalue=True,
            offvalue=False,
            command=lambda: self._save_stems_default(self._stems_default_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=40,
            height=22,
        )
        self._stems_default_switch.pack()

        # AI metadata toggle
        self._field_label(
            body,
            6,
            "AI title extraction  (✦ DeepSeek)",
            "Sends the YouTube video title to DeepSeek to recover the original artist "
            "and song title. Requires a DeepSeek API key — set it in API keys below. "
            "Completed jobs show a ✦ AI badge when this was used.",
        )

        ai_toggle_row = ctk.CTkFrame(body, fg_color="transparent")
        ai_toggle_row.grid(row=7, column=0, sticky="w", pady=(0, t.space.xs))

        self._ai_metadata_var = ctk.BooleanVar(value=True)
        self._ai_metadata_switch = ctk.CTkSwitch(
            ai_toggle_row,
            text="",
            variable=self._ai_metadata_var,
            onvalue=True,
            offvalue=False,
            command=lambda: self._save_ai_metadata(self._ai_metadata_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=40,
            height=22,
        )
        self._ai_metadata_switch.pack()

        return row + 3

    # ── Stem separation section ──

    def _build_stems_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent,
            row,
            "Stem separation",
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
            width=436,
            height=38,
        )
        self._stem_model_dropdown.pack(padx=2, pady=2)

        # Device dropdown
        self._field_label(
            body,
            2,
            "Compute device",
            "'Auto' picks the best available. "
            "Apple Silicon users: MPS is significantly faster than CPU.",
        )

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
            width=256,
            height=38,
        )
        self._device_dropdown.pack(padx=2, pady=2)

        return row + 3

    # ── Discovery behavior section ──

    def _build_discovery_behavior_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent,
            row,
            "Sample discovery",
            "Defaults for the Digital Crate reel, genre weighting, and preview.",
        )

        self._field_label(
            body, 0, "Minimum owners (have)",
            "Discogs 'have' threshold — lower surfaces rarer records.",
        )
        self._min_have_entry = ctk.CTkEntry(
            body, width=100, height=38,
            **{k: v for k, v in style_input(t).items() if k != "height"},
            justify="center",
        )
        self._min_have_entry.grid(row=1, column=0, sticky="w", pady=(0, t.space.md))
        self._min_have_entry.bind(
            "<FocusOut>", lambda _e: self._save_min_have(), add="+",
        )
        self._min_have_entry.bind(
            "<Return>", lambda _e: self._save_min_have(), add="+",
        )

        self._field_label(
            body, 2, "Maximum collectors (avoid mainstream hits)",
            "Discogs 'have' ceiling — records above this are excluded as "
            "too common/popular to be a 'gem'. Set high to disable.",
        )
        self._max_have_entry = ctk.CTkEntry(
            body, width=100, height=38,
            **{k: v for k, v in style_input(t).items() if k != "height"},
            justify="center",
        )
        self._max_have_entry.grid(row=3, column=0, sticky="w", pady=(0, t.space.md))
        self._max_have_entry.bind(
            "<FocusOut>", lambda _e: self._save_max_have(), add="+",
        )
        self._max_have_entry.bind(
            "<Return>", lambda _e: self._save_max_have(), add="+",
        )

        self._field_label(body, 4, "Reel size", "How many cards each Dig fills.")
        _reel_wrapper = ctk.CTkFrame(
            body, fg_color=t.border.strong, border_width=0, corner_radius=t.radius.md,
        )
        _reel_wrapper.grid(row=5, column=0, sticky="w", pady=(0, t.space.md))
        self._reel_size_dropdown = ctk.CTkOptionMenu(
            _reel_wrapper,
            values=[str(n) for n in (4, 6, 8, 10, 12, 16)],
            command=self._save_reel_size,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=120,
            height=38,
        )
        self._reel_size_dropdown.pack(padx=2, pady=2)

        self._field_label(
            body, 6, "Prioritize sample-friendly genres",
            "Tilts the roulette toward funk, soul, jazz, Greek gems, etc. "
            "Nothing is ever fully excluded.",
        )
        prio_row = ctk.CTkFrame(body, fg_color="transparent")
        prio_row.grid(row=7, column=0, sticky="w", pady=(0, t.space.sm))
        self._prioritize_var = ctk.BooleanVar(value=True)
        self._prioritize_switch = ctk.CTkSwitch(
            prio_row, text="", variable=self._prioritize_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_prioritize_samples(self._prioritize_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated, width=40, height=22,
        )
        self._prioritize_switch.pack(side="left")

        self._field_label(body, 8, "Sample-weight intensity")
        intensity_row = ctk.CTkFrame(body, fg_color="transparent")
        intensity_row.grid(row=9, column=0, sticky="ew", pady=(0, t.space.md))
        intensity_row.grid_columnconfigure(0, weight=1)
        self._intensity_slider = ctk.CTkSlider(
            intensity_row, from_=0.0, to=1.0, number_of_steps=20,
            command=self._on_intensity_slider,
            progress_color=t.accent.blue,
            button_color=t.accent.blue,
            fg_color=t.surface.elevated,
        )
        self._intensity_slider.grid(row=0, column=0, sticky="ew", padx=(0, t.space.md))
        self._intensity_label = ctk.CTkLabel(
            intensity_row, text="0.60", text_color=t.text.secondary,
            font=t.font.mono_body, width=48,
        )
        self._intensity_label.grid(row=0, column=1)

        self._field_label(
            body, 10, "Allow compilations",
            "Include Various Artists comps — many breaks live on them.",
        )
        comp_row = ctk.CTkFrame(body, fg_color="transparent")
        comp_row.grid(row=11, column=0, sticky="w", pady=(0, t.space.md))
        self._compilations_var = ctk.BooleanVar(value=False)
        self._compilations_switch = ctk.CTkSwitch(
            comp_row, text="", variable=self._compilations_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_allow_compilations(self._compilations_var.get()),
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated, width=40, height=22,
        )
        self._compilations_switch.pack(side="left")

        self._field_label(body, 12, "Preview volume")
        pv_row = ctk.CTkFrame(body, fg_color="transparent")
        pv_row.grid(row=13, column=0, sticky="ew", pady=(0, t.space.md))
        pv_row.grid_columnconfigure(0, weight=1)
        self._preview_volume_slider = ctk.CTkSlider(
            pv_row, from_=0.0, to=1.0, number_of_steps=20,
            command=self._on_preview_volume_slider,
            progress_color=t.accent.purple,
            button_color=t.accent.purple,
            fg_color=t.surface.elevated,
        )
        self._preview_volume_slider.grid(row=0, column=0, sticky="ew", padx=(0, t.space.md))
        self._preview_volume_label = ctk.CTkLabel(
            pv_row, text="85%", text_color=t.text.secondary,
            font=t.font.mono_body, width=48,
        )
        self._preview_volume_label.grid(row=0, column=1)

        self._field_label(
            body, 14, "Warm previews after Dig",
            "Background-download quick previews so Play is usually instant.",
        )
        prefetch_row = ctk.CTkFrame(body, fg_color="transparent")
        prefetch_row.grid(row=15, column=0, sticky="w", pady=(0, t.space.sm))
        self._prefetch_enabled_var = ctk.BooleanVar(value=True)
        self._prefetch_enabled_switch = ctk.CTkSwitch(
            prefetch_row, text="", variable=self._prefetch_enabled_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_prefetch_enabled(
                self._prefetch_enabled_var.get(),
            ),
            progress_color=t.accent.purple,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated, width=40, height=22,
        )
        self._prefetch_enabled_switch.pack(side="left")

        self._field_label(body, 16, "Prefetch concurrency")
        _pf_wrapper = ctk.CTkFrame(
            body, fg_color=t.border.strong, border_width=0,
            corner_radius=t.radius.md,
        )
        _pf_wrapper.grid(row=17, column=0, sticky="w", pady=(0, t.space.md))
        self._prefetch_concurrency_dropdown = ctk.CTkOptionMenu(
            _pf_wrapper,
            values=[str(n) for n in range(1, 5)],
            command=self._save_prefetch_concurrency,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=120,
            height=38,
        )
        self._prefetch_concurrency_dropdown.pack(padx=2, pady=2)

        self._field_label(
            body, 18, "Keep decoded previews in memory",
            "LRU cache (8 tracks) for instant waveform on Preview click.",
        )
        keep_row = ctk.CTkFrame(body, fg_color="transparent")
        keep_row.grid(row=19, column=0, sticky="w", pady=(0, t.space.md))
        self._prefetch_keep_decoded_var = ctk.BooleanVar(value=True)
        self._prefetch_keep_decoded_switch = ctk.CTkSwitch(
            keep_row, text="", variable=self._prefetch_keep_decoded_var,
            onvalue=True, offvalue=False,
            command=lambda: self._save_prefetch_keep_decoded(
                self._prefetch_keep_decoded_var.get(),
            ),
            progress_color=t.accent.purple,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated, width=40, height=22,
        )
        self._prefetch_keep_decoded_switch.pack(side="left")

        self._discovery_health_label = ctk.CTkLabel(
            body, text="", text_color=t.text.muted,
            font=t.font.caption, anchor="w", justify="left", wraplength=680,
        )
        self._discovery_health_label.grid(row=20, column=0, sticky="w")

        return row + 3

    def _build_export_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent, row, "MPC export",
            "Default WAV format for Export to MPC and chop-kit exports.",
        )

        self._field_label(body, 0, "Sample rate")
        _sr_wrapper = ctk.CTkFrame(
            body, fg_color=t.border.strong, border_width=0, corner_radius=t.radius.md,
        )
        _sr_wrapper.grid(row=1, column=0, sticky="w", pady=(0, t.space.md))
        self._export_sr_dropdown = ctk.CTkOptionMenu(
            _sr_wrapper,
            values=["44100 Hz", "48000 Hz"],
            command=self._save_export_sample_rate,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=160,
            height=38,
        )
        self._export_sr_dropdown.pack(padx=2, pady=2)

        self._field_label(body, 2, "Bit depth")
        _bits_wrapper = ctk.CTkFrame(
            body, fg_color=t.border.strong, border_width=0, corner_radius=t.radius.md,
        )
        _bits_wrapper.grid(row=3, column=0, sticky="w")
        self._export_bits_dropdown = ctk.CTkOptionMenu(
            _bits_wrapper,
            values=["16-bit", "24-bit"],
            command=self._save_export_bit_depth,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            width=160,
            height=38,
        )
        self._export_bits_dropdown.pack(padx=2, pady=2)

        return row + 3

    # ── Discovery & AI section ──

    def _build_discovery_section(self, parent, row: int) -> int:
        t = self._theme
        body = self._section_card(
            parent,
            row,
            "API keys",
            "Credentials for Discogs discovery and DeepSeek AI title extraction.",
        )

        # ── Discogs token ──
        self._field_label(body, 0, "Discogs personal access token",
                          "Required for the Digital Crate 'Dig' feature.")

        token_row = ctk.CTkFrame(body, fg_color="transparent")
        token_row.grid(row=1, column=0, sticky="ew", pady=(0, t.space.xs))
        token_row.grid_columnconfigure(0, weight=1)

        self._token_entry = GlowEntry(
            token_row,
            t,
            placeholder="Paste token (stored in OS keyring or local file)",
            show_clear_button=True,
        )
        self._token_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))

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
            token_row,
            text="Get token",
            command=lambda: webbrowser.open(
                "https://www.discogs.com/settings/developers",
            ),
            **style_ghost_button(t),
        ).grid(row=0, column=1, sticky="e")

        self._token_status_label = ctk.CTkLabel(
            body,
            text="",
            text_color=t.text.muted,
            font=t.font.caption,
            anchor="w",
        )
        self._token_status_label.grid(row=2, column=0, sticky="w", pady=(0, t.space.xs))

        self._keyring_warning_label = ctk.CTkLabel(
            body,
            text=(
                "⚠  OS keyring is unavailable on this system. "
                "Keys will be stored in plaintext files restricted to your user account."
            ),
            text_color=t.status.warning,
            font=t.font.caption,
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self._keyring_warning_label.grid(row=3, column=0, sticky="w",
                                          pady=(0, t.space.lg))
        self._keyring_warning_label.grid_remove()

        # ── DeepSeek API key ──
        self._field_label(body, 4, "DeepSeek API key  (✦ AI title extraction)",
                          "Requires a key from platform.deepseek.com — only DeepSeek keys work here. "
                          "Takes effect immediately; no restart required.")

        ds_row = ctk.CTkFrame(body, fg_color="transparent")
        ds_row.grid(row=5, column=0, sticky="ew", pady=(0, t.space.xs))
        ds_row.grid_columnconfigure(0, weight=1)

        self._deepseek_entry = GlowEntry(
            ds_row,
            t,
            placeholder="DeepSeek key  (sk-…)",
            show_clear_button=True,
        )
        self._deepseek_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))

        self._deepseek_entry._entry.bind(
            "<FocusOut>",
            lambda _e: self._save_deepseek_key(self._deepseek_entry.get()),
            add="+",
        )
        self._deepseek_entry._entry.bind(
            "<Return>",
            lambda _e: self._save_deepseek_key(self._deepseek_entry.get()),
            add="+",
        )

        ctk.CTkButton(
            ds_row,
            text="Get key",
            command=lambda: webbrowser.open("https://platform.deepseek.com/api_keys"),
            **style_ghost_button(t),
        ).grid(row=0, column=1, sticky="e")

        self._deepseek_status_label = ctk.CTkLabel(
            body,
            text="",
            text_color=t.text.muted,
            font=t.font.caption,
            anchor="w",
        )
        self._deepseek_status_label.grid(row=6, column=0, sticky="w")

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
            ver_row,
            text="Version",
            text_color=t.text.secondary,
            font=t.font.caption,
            width=140,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            ver_row,
            text="0.1.0",
            text_color=t.text.primary,
            font=t.font.mono_body,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")

        # Row 1: log path
        log_row = ctk.CTkFrame(body, fg_color="transparent")
        log_row.grid(row=1, column=0, sticky="ew", pady=(0, t.space.md))
        log_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            log_row,
            text="Log file",
            text_color=t.text.secondary,
            font=t.font.caption,
            width=140,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        log_path = str(self._config.data_dir / "cratedigger.log")
        ctk.CTkLabel(
            log_row,
            text=log_path,
            text_color=t.text.primary,
            font=t.font.mono_body,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkButton(
            log_row,
            text="Open log",
            command=lambda: self._open_path(log_path),
            **style_ghost_button(t),
        ).grid(row=0, column=2, sticky="e")

        # Row 2: preview cache
        cache_row = ctk.CTkFrame(body, fg_color="transparent")
        cache_row.grid(row=2, column=0, sticky="ew", pady=(t.space.md, 0))
        cache_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            cache_row,
            text="Preview cache",
            text_color=t.text.secondary,
            font=t.font.caption,
            width=140,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            cache_row,
            text="Downloaded audio cached for in-app previews. "
                 "Stale entries are also cleared automatically on startup.",
            text_color=t.text.primary,
            font=t.font.body,
            anchor="w",
            wraplength=460,
            justify="left",
        ).grid(row=0, column=1, sticky="w")

        ctk.CTkButton(
            cache_row,
            text="Clear cache",
            command=self._clear_preview_cache,
            **style_ghost_button(t),
        ).grid(row=0, column=2, sticky="e")

        # Row 3: reset defaults
        reset_row = ctk.CTkFrame(body, fg_color="transparent")
        reset_row.grid(row=3, column=0, sticky="ew", pady=(t.space.lg, 0))

        ctk.CTkButton(
            reset_row,
            text="Reset all settings to defaults",
            command=self._confirm_reset,
            **style_danger_button(t),
        ).pack(anchor="w")

        return row + 1

    def _clear_preview_cache(self) -> None:
        if self._ctx.preview is None:
            self._ctx.publish_toast("Preview cache is unavailable.", "error")
            return
        try:
            removed = self._ctx.preview.clear_cache()
        except Exception as e:
            self._ctx.publish_toast(f"Could not clear cache: {e}", "error")
            return
        self._ctx.publish_toast(
            f"Cleared {removed} cached preview file{'s' if removed != 1 else ''}.",
            "success" if removed else "info",
        )

    # ── Initial population from config ──

    def _populate_from_config(self) -> None:
        snap = self._config.snapshot()
        cfg = snap.config

        if self._vault_entry is not None:
            self._vault_entry.set(cfg.general.vault_root)

        if self._staging_entry is not None:
            self._staging_entry.set(cfg.general.staging_root)

        if self._mpc_root_entry is not None:
            self._mpc_root_entry.set(cfg.general.mpc_samples_root)

        if self._workers_dropdown is not None:
            self._workers_dropdown.set(str(cfg.general.concurrent_workers))

        if self._mpc_export_workers_dropdown is not None:
            self._mpc_export_workers_dropdown.set(
                str(cfg.general.mpc_export_max_concurrent),
            )

        if self._stems_default_var is not None:
            self._stems_default_var.set(cfg.general.enable_stems_by_default)

        if self._ai_metadata_var is not None:
            self._ai_metadata_var.set(cfg.general.use_ai_metadata)

        if self._folder_scheme_dropdown is not None:
            label = self._label_for_folder_scheme(cfg.general.vault_folder_scheme)
            self._folder_scheme_dropdown.set(label)

        if self._stem_model_dropdown is not None:
            label = self._label_for_stem_model(cfg.stems.model)
            self._stem_model_dropdown.set(label)

        if self._device_dropdown is not None:
            label = self._label_for_device(cfg.stems.device)
            self._device_dropdown.set(label)

        if self._token_entry is not None and snap.discogs_token:
            self._token_entry.set("•" * 20)

        if self._deepseek_entry is not None and snap.deepseek_key:
            self._deepseek_entry.set("•" * 20)

        disc = cfg.discovery
        if self._min_have_entry is not None:
            self._min_have_entry.delete(0, "end")
            self._min_have_entry.insert(0, str(disc.default_min_have))
        if self._max_have_entry is not None:
            self._max_have_entry.delete(0, "end")
            self._max_have_entry.insert(0, str(disc.max_have))
        if self._reel_size_dropdown is not None:
            self._reel_size_dropdown.set(str(disc.reel_size))
        if self._prioritize_var is not None:
            self._prioritize_var.set(disc.prioritize_samples)
        if self._intensity_slider is not None:
            self._intensity_slider.set(disc.sample_weight_intensity)
            self._update_intensity_label(disc.sample_weight_intensity)
        if self._compilations_var is not None:
            self._compilations_var.set(disc.allow_compilations)
        if self._preview_volume_slider is not None:
            self._preview_volume_slider.set(cfg.ui.preview_volume)
            self._update_preview_volume_label(cfg.ui.preview_volume)
        if self._prefetch_enabled_var is not None:
            self._prefetch_enabled_var.set(disc.preview_prefetch_enabled)
        if self._prefetch_concurrency_dropdown is not None:
            self._prefetch_concurrency_dropdown.set(
                str(disc.preview_prefetch_concurrency),
            )
        if self._prefetch_keep_decoded_var is not None:
            self._prefetch_keep_decoded_var.set(disc.preview_prefetch_keep_decoded)

        exp = cfg.export
        if self._export_sr_dropdown is not None:
            self._export_sr_dropdown.set(
                "48000 Hz" if exp.sample_rate == 48000 else "44100 Hz"
            )
        if self._export_bits_dropdown is not None:
            self._export_bits_dropdown.set(
                "24-bit" if exp.bit_depth == 24 else "16-bit"
            )

        self._refresh_discovery_health()

        # Keyring warning visibility
        if self._keyring_warning_label is not None:
            if snap.keyring_available:
                self._keyring_warning_label.grid_remove()
            else:
                self._keyring_warning_label.grid()

        # Status labels
        self._refresh_token_status(snap)
        self._refresh_deepseek_status(snap)

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
                f"Could not create folder: {e}",
                kind="error",
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
                f"Could not create folder: {e}",
                kind="error",
            )
            return
        try:
            self._config.update_general(staging_root=str(Path(value).expanduser()))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_mpc_root(self, value: str) -> None:
        value = self._normalize_path(value)
        if not value:
            return
        try:
            # Best-effort mkdir — on a not-yet-inserted SD card this will
            # fail, which is fine; the folder just needs to exist by the
            # time the user actually clicks "MPC Workflow".
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._ctx.publish_toast(
                f"Could not create folder: {e}",
                kind="error",
            )
            return
        try:
            self._config.update_general(mpc_samples_root=str(Path(value).expanduser()))
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

    def _save_mpc_export_workers(self, value: str) -> None:
        try:
            n = int(value)
        except ValueError:
            return
        try:
            self._config.update_general(mpc_export_max_concurrent=n)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            mgr = self._ctx.mpc_export_manager
            if mgr is not None:
                mgr.set_max_workers(n)
            self._ctx.notify_config_changed()

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

    def _save_folder_scheme(self, label: str) -> None:
        key = self._folder_scheme_key_from_label(label)
        if key is None:
            return
        try:
            self._config.update_general(vault_folder_scheme=key)
            self._ctx.publish_toast(
                "Folder scheme updated. New tracks will use this layout.",
                kind="info",
            )
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

    def _save_min_have(self) -> None:
        if self._min_have_entry is None:
            return
        try:
            n = int(self._min_have_entry.get().strip())
        except ValueError:
            return
        try:
            self._config.update_discovery(default_min_have=n)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _save_max_have(self) -> None:
        if self._max_have_entry is None:
            return
        try:
            n = int(self._max_have_entry.get().strip())
        except ValueError:
            return
        try:
            self._config.update_discovery(max_have=n)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _save_reel_size(self, value: str) -> None:
        try:
            n = int(value)
        except ValueError:
            return
        try:
            self._config.update_discovery(reel_size=n)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _save_prioritize_samples(self, value: bool) -> None:
        try:
            self._config.update_discovery(prioritize_samples=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _on_intensity_slider(self, value: float) -> None:
        self._update_intensity_label(value)
        self._debounce(
            "sample_intensity",
            lambda: self._save_sample_intensity(float(value)),
        )

    def _save_sample_intensity(self, value: float) -> None:
        try:
            self._config.update_discovery(sample_weight_intensity=round(value, 2))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _update_intensity_label(self, value: float) -> None:
        if self._intensity_label is not None:
            self._intensity_label.configure(text=f"{value:.2f}")

    def _save_allow_compilations(self, value: bool) -> None:
        try:
            self._config.update_discovery(allow_compilations=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _save_prefetch_enabled(self, value: bool) -> None:
        try:
            self._config.update_discovery(preview_prefetch_enabled=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            self._ctx.notify_config_changed()

    def _save_prefetch_concurrency(self, value: str) -> None:
        try:
            n = int(value)
        except ValueError:
            return
        try:
            self._config.update_discovery(preview_prefetch_concurrency=n)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            pf = self._ctx.preview_prefetch
            if pf is not None:
                pf.configure(max_workers=n)
            self._ctx.notify_config_changed()

    def _save_prefetch_keep_decoded(self, value: bool) -> None:
        try:
            self._config.update_discovery(preview_prefetch_keep_decoded=bool(value))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
        else:
            pf = self._ctx.preview_prefetch
            if pf is not None:
                pf.configure(keep_decoded=bool(value))
            self._ctx.notify_config_changed()

    def _on_preview_volume_slider(self, value: float) -> None:
        self._update_preview_volume_label(value)
        self._debounce(
            "preview_volume",
            lambda: self._save_preview_volume(float(value)),
        )

    def _save_preview_volume(self, value: float) -> None:
        try:
            self._config.update_ui(preview_volume=round(value, 2))
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _update_preview_volume_label(self, value: float) -> None:
        if self._preview_volume_label is not None:
            self._preview_volume_label.configure(text=f"{int(value * 100)}%")

    def _save_export_sample_rate(self, label: str) -> None:
        sr = 48000 if "48000" in label else 44100
        try:
            self._config.update_export(sample_rate=sr)
            if self._ctx.exporter is not None:
                self._ctx.exporter.update_format(sample_rate=sr)
            self._ctx.publish_toast(
                f"Export sample rate set to {sr} Hz.",
                kind="info",
            )
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _save_export_bit_depth(self, label: str) -> None:
        bits = 24 if "24" in label else 16
        try:
            self._config.update_export(bit_depth=bits)
            if self._ctx.exporter is not None:
                self._ctx.exporter.update_format(bit_depth=bits)
            self._ctx.publish_toast(
                f"Export bit depth set to {bits}-bit.",
                kind="info",
            )
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")

    def _refresh_discovery_health(self) -> None:
        t = self._theme
        if self._discovery_health_label is None:
            return
        eng = self._ctx.discovery
        if eng is None:
            self._discovery_health_label.configure(
                text="Discovery health: no Discogs token — Dig is disabled.",
                text_color=t.text.muted,
            )
            return
        try:
            stats = eng.get_stats()
        except Exception:
            self._discovery_health_label.configure(
                text="Discovery health: unavailable.",
                text_color=t.text.muted,
            )
            return
        err_note = ""
        if stats.throttle_events:
            err_note = f" · {stats.throttle_events} throttle event(s)"
        self._discovery_health_label.configure(
            text=(
                f"Discovery health: Discogs {stats.discogs_requests} req "
                f"({stats.discogs_rate_waits:.1f}s waited) · "
                f"YTM {stats.ytm_requests} req "
                f"({stats.ytm_rate_waits:.1f}s waited){err_note}"
            ),
            text_color=t.status.success if stats.throttle_events == 0 else t.status.warning,
        )

    def _save_discogs_token(self, value: str) -> None:
        value = (value or "").strip()
        if value and set(value) == {"•"}:
            return

        try:
            snap = self._config.set_discogs_token(value or None)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
            return

        engine_ready = False
        if snap.discogs_token:
            if self._ctx.discovery is not None:
                try:
                    self._ctx.discovery.update_discogs_token(snap.discogs_token)
                    engine_ready = True
                except Exception:
                    self._log.exception("Failed to update DiscoveryEngine token")
            else:
                try:
                    from core.discovery import DiscoveryEngine
                    self._ctx.discovery = DiscoveryEngine(
                        db=self._ctx.database,
                        discogs_token=snap.discogs_token,
                        logger=self._log.getChild("discovery"),
                    )
                    engine_ready = True
                    self._log.info("DiscoveryEngine created after first token save.")
                except Exception:
                    self._log.exception("Failed to create DiscoveryEngine")
            self._token_entry.set("•" * 20)
            if engine_ready:
                self._ctx.publish_toast(
                    "Discogs token saved. Digital Crate is ready to dig.",
                    kind="success",
                )
            else:
                self._ctx.publish_toast(
                    "Token saved but discovery failed to start. "
                    "Check your token and try again.",
                    kind="error",
                )
        else:
            if self._ctx.discovery is not None:
                try:
                    self._ctx.discovery.update_discogs_token(None)
                except Exception:
                    pass
            self._token_entry.set("")
            self._ctx.publish_toast("Discogs token cleared.", kind="info")

        self._refresh_token_status(snap)
        self._refresh_discovery_health()
        self._ctx.notify_config_changed()

    def _save_deepseek_key(self, value: str) -> None:
        value = (value or "").strip()
        if value and set(value) == {"•"}:
            return

        try:
            snap = self._config.set_deepseek_key(value or None)
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
            return

        # Rebuild the AI enricher and push it to the pipeline immediately.
        if self._ctx.pipeline is not None:
            try:
                from core.ai_metadata import make_ai_enricher
                enricher = make_ai_enricher(
                    api_key=snap.deepseek_key or None,
                    logger=self._log.getChild("ai_metadata"),
                )
                self._ctx.pipeline.update_ai_enricher(enricher)
            except Exception:
                self._log.exception("Failed to update AI enricher")

        if value:
            self._deepseek_entry.set("•" * 20)
            self._ctx.publish_toast(
                "DeepSeek key saved. AI title extraction is now active.",
                kind="success",
            )
        else:
            self._deepseek_entry.set("")
            self._ctx.publish_toast("DeepSeek key cleared.", kind="info")

        self._refresh_deepseek_status(snap)
        self._ctx.notify_config_changed()

    def on_tab_visible(self) -> None:
        """Refresh health readouts when returning to Settings."""
        self._refresh_discovery_health()
        snap = self._config.snapshot()
        self._refresh_token_status(snap)
        self._refresh_deepseek_status(snap)

    # ── Browse buttons ──

    def _open_vault_root(self) -> None:
        self._open_directory(self._vault_entry.get() if self._vault_entry else "", "Vault root")

    def _open_mpc_root(self) -> None:
        self._open_directory(
            self._mpc_root_entry.get() if self._mpc_root_entry else "",
            "MPC Samples folder",
        )

    def _open_directory(self, raw_path: str, label: str) -> None:
        value = self._normalize_path(raw_path)
        if not value:
            self._ctx.publish_toast(f"Enter a {label.lower()} path first.", "warning")
            return

        path = Path(value).expanduser()
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._ctx.publish_toast(f"Could not open {label}: {e}", "error")
                return

        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as e:
            try:
                webbrowser.open(path.as_uri())
            except Exception:
                self._ctx.publish_toast(f"Could not open folder: {e}", "error")

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

    def _pick_mpc_root(self) -> None:
        current = self._mpc_root_entry.get() if self._mpc_root_entry else ""
        initial = str(Path(current).expanduser()) if current else str(Path.home())
        chosen = filedialog.askdirectory(
            title="Choose MPC Samples folder",
            initialdir=initial,
            mustexist=False,
        )
        if chosen:
            self._mpc_root_entry.set(chosen)
            self._save_mpc_root(chosen)

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
                mpc_export_max_concurrent=1,
                enable_stems_by_default=False,
                use_ai_metadata=True,
                vault_folder_scheme="genre/bpm_key_artist_title",
                # Note: has_deepseek_key is not reset — credentials are preserved.
            )
            self._config.update_stems(model="htdemucs_ft", device="auto")
            self._config.update_downloader(
                retries=5,
                fragment_retries=5,
                concurrent_fragments=4,
            )
            self._config.update_discovery(
                default_min_have=10,
                max_have=3000,
                reel_size=8,
                prioritize_samples=True,
                sample_weight_intensity=0.6,
                allow_compilations=False,
                preview_prefetch_enabled=True,
                preview_prefetch_concurrency=2,
                preview_prefetch_keep_decoded=True,
            )
            self._config.update_export(sample_rate=44100, bit_depth=16)
            self._config.update_ui(preview_volume=0.85)
            # Don't clear the Discogs token on reset — user would lose
            # it and have to re-enter. Reset is for *preferences*, not
            # credentials.
        except ConfigError as e:
            self._ctx.publish_toast(str(e), kind="error")
            return

        self._populate_from_config()
        self._ctx.publish_toast(
            "Settings reset to defaults.",
            kind="success",
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
                    text="⚠  Token stored in plaintext fallback (keyring unavailable).",
                    text_color=t.status.warning,
                )
        else:
            self._token_status_label.configure(
                text="No token set. Digital Crate discovery will be disabled.",
                text_color=t.text.muted,
            )

    def _refresh_deepseek_status(self, snap) -> None:
        t = self._theme
        if self._deepseek_status_label is None:
            return

        if snap.deepseek_key:
            if snap.keyring_available:
                self._deepseek_status_label.configure(
                    text="✓  Key stored securely in OS keyring. AI title extraction is active.",
                    text_color=t.status.success,
                )
            else:
                self._deepseek_status_label.configure(
                    text="⚠  Key stored in plaintext fallback. AI title extraction is active.",
                    text_color=t.status.warning,
                )
        else:
            self._deepseek_status_label.configure(
                text="No key set. AI title extraction will be disabled.",
                text_color=t.text.muted,
            )

    # ── Helpers ──

    def _field_label(
        self,
        parent,
        row: int,
        title: str,
        description: str = "",
    ) -> None:
        """Render a form-field title + optional description block."""
        t = self._theme

        ctk.CTkLabel(
            parent,
            text=title,
            **style_label_body(t),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=(t.space.xs, 0))

        if description:
            ctk.CTkLabel(
                parent,
                text=description,
                **style_label_meta(t),
                wraplength=680,
                justify="left",
                anchor="w",
            ).grid(row=row, column=0, sticky="w", pady=(18, t.space.xs))
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

    @staticmethod
    def _label_for_folder_scheme(scheme_key: str) -> str:
        for key, label in VAULT_FOLDER_SCHEMES:
            if key == scheme_key:
                return label
        return VAULT_FOLDER_SCHEMES[0][1]

    @staticmethod
    def _folder_scheme_key_from_label(label: str) -> Optional[str]:
        for key, scheme_label in VAULT_FOLDER_SCHEMES:
            if scheme_label == label:
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
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.xl)

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
            anchor="w",
            justify="left",
            wraplength=380,
        ).pack(anchor="w", pady=(t.space.sm, t.space.xl))

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x")

        ctk.CTkButton(
            buttons,
            text="Cancel",
            command=self._cancel,
            **style_secondary_button(t),
        ).pack(side="right", padx=(t.space.sm, 0))

        ctk.CTkButton(
            buttons,
            text="Reset",
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

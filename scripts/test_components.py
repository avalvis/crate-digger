"""Interactive smoke test for Phase 3 components.

Boots a test window showing all components side-by-side so you can
visually verify hover states, animations, scroll smoothness, and
event wiring.
"""
from __future__ import annotations

import customtkinter as ctk
import random
import time

from core.pipeline import PipelineStage
from core.queue_manager import QueueEvent, QueueEventType
from ui.components.data_table import Column, SortDirection, VirtualDataTable
from ui.components.glow_entry import GlowEntry
from ui.components.progress_row import ProgressRow
from ui.components.sidebar import SidebarNavButton
from ui.components.spinner import Spinner
from ui.components.toast import ToastLayer
from ui.theme import (
    apply_customtkinter_globals,
    build_theme,
    style_primary_button,
    style_secondary_button,
    style_label_heading,
    style_label_subheading,
    style_label_meta,
)


def main() -> int:
    apply_customtkinter_globals(None)  # type: ignore[arg-type]
    theme = build_theme()

    root = ctk.CTk()
    root.title("Crate Digger — Component Test")
    root.geometry("1500x900")
    root.configure(fg_color=theme.surface.app)
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(0, weight=1)

    # ── Left: sidebar nav demo ──
    sidebar = ctk.CTkFrame(
        root, width=220, fg_color=theme.surface.base, corner_radius=0,
    )
    sidebar.grid(row=0, column=0, sticky="ns")
    sidebar.grid_propagate(False)

    nav_items = [
        SidebarNavButton(sidebar, theme, label="Manual Rip", icon_glyph="▸"),
        SidebarNavButton(sidebar, theme, label="Digital Crate", icon_glyph="◆",
                         badge_text="3"),
        SidebarNavButton(sidebar, theme, label="Vault", icon_glyph="▤"),
        SidebarNavButton(sidebar, theme, label="Settings", icon_glyph="⚙"),
    ]

    def make_activator(index: int):
        def activate():
            for i, item in enumerate(nav_items):
                item.set_active(i == index)
        return activate

    for i, item in enumerate(nav_items):
        item._command = make_activator(i)   # type: ignore[attr-defined]
        item.pack(fill="x", padx=theme.space.md, pady=(0, theme.space.xs),
                  anchor="w")
    nav_items[0].set_active(True)

    # ── Right: content area with demos of each component ──
    content = ctk.CTkScrollableFrame(
        root, fg_color=theme.surface.app, corner_radius=0,
    )
    content.grid(row=0, column=1, sticky="nsew")
    content.grid_columnconfigure(0, weight=1)

    toast_layer = ToastLayer(content, theme)

    # Section: glow entry
    ctk.CTkLabel(content, text="GlowEntry", **style_label_heading(theme)).pack(
        anchor="w", padx=theme.space.xl, pady=(theme.space.xl, theme.space.md),
    )

    url_row = ctk.CTkFrame(content, fg_color="transparent")
    url_row.pack(fill="x", padx=theme.space.xl, pady=(0, theme.space.md))

    def validate_url(value: str) -> bool:
        return value.startswith("http://") or value.startswith("https://")

    url_entry = GlowEntry(
        url_row, theme,
        placeholder="Paste a YouTube / YouTube Music URL…",
        prefix_icon="▸",
        validator=validate_url,
        error_message="URL must start with http:// or https://",
        on_submit=lambda v: toast_layer.show(
            f"Submitted: {v}", kind="success",
        ),
        width=520,
    )
    url_entry.pack(side="left")

    ctk.CTkButton(
        url_row, text="Queue",
        command=lambda: toast_layer.show(
            f"Queued: {url_entry.get()}", kind="info",
            action_label="Undo",
            action_callback=lambda: toast_layer.show("Undone", kind="warning"),
        ),
        **style_primary_button(theme),
    ).pack(side="left", padx=(theme.space.md, 0))

    search_entry = GlowEntry(
        content, theme,
        placeholder="Search the vault…",
        prefix_icon="⌕",
        width=400,
    )
    search_entry.pack(anchor="w", padx=theme.space.xl, pady=(0, theme.space.xl))

    # Section: toasts
    ctk.CTkLabel(content, text="Toasts", **style_label_heading(theme)).pack(
        anchor="w", padx=theme.space.xl, pady=(0, theme.space.md),
    )
    toast_row = ctk.CTkFrame(content, fg_color="transparent")
    toast_row.pack(fill="x", padx=theme.space.xl, pady=(0, theme.space.xl))

    for kind, label in [("info", "Info"), ("success", "Success"),
                         ("warning", "Warning"), ("error", "Error")]:
        ctk.CTkButton(
            toast_row, text=label,
            command=lambda k=kind, l=label: toast_layer.show(
                f"{l} toast at {time.strftime('%H:%M:%S')}", kind=k,
            ),
            **style_secondary_button(theme),
        ).pack(side="left", padx=(0, theme.space.sm))

    ctk.CTkButton(
        toast_row, text="Toast with action",
        command=lambda: toast_layer.show(
            "Export failed: destination unmounted",
            kind="error",
            action_label="Retry",
            action_callback=lambda: toast_layer.show("Retrying…", kind="info"),
        ),
        **style_secondary_button(theme),
    ).pack(side="left", padx=(0, theme.space.sm))

    # Section: spinners
    ctk.CTkLabel(content, text="Spinner", **style_label_heading(theme)).pack(
        anchor="w", padx=theme.space.xl, pady=(0, theme.space.md),
    )
    spin_row = ctk.CTkFrame(content, fg_color="transparent")
    spin_row.pack(fill="x", padx=theme.space.xl, pady=(0, theme.space.xl))

    for size, label in [("sm", "Small"), ("md", "Medium"), ("lg", "Large")]:
        cell = ctk.CTkFrame(spin_row, fg_color="transparent")
        cell.pack(side="left", padx=(0, theme.space.xl))
        s = Spinner(cell, theme, size=size)
        s.pack()
        s.start()
        ctk.CTkLabel(cell, text=label, **style_label_meta(theme)).pack(
            pady=(theme.space.xs, 0),
        )

    # Section: progress row
    ctk.CTkLabel(content, text="Progress Row", **style_label_heading(theme)).pack(
        anchor="w", padx=theme.space.xl, pady=(0, theme.space.md),
    )

    progress_container = ctk.CTkFrame(content, fg_color="transparent")
    progress_container.pack(fill="x", padx=theme.space.xl,
                            pady=(0, theme.space.xl))

    row1 = ProgressRow(
        progress_container, theme,
        job_id=1, source_url="https://music.youtube.com/watch?v=abc123",
        on_cancel=lambda jid: print(f"cancel {jid}"),
    )
    row1.pack(fill="x", pady=(0, theme.space.sm))

    row2 = ProgressRow(
        progress_container, theme,
        job_id=2, source_url="https://music.youtube.com/watch?v=def456",
        is_discovery=True,
        on_cancel=lambda jid: print(f"cancel {jid}"),
    )
    row2.pack(fill="x", pady=(0, theme.space.sm))

    # Animate progress on row1
    def simulate_progress():
        stages = [
            (PipelineStage.DOWNLOADING, "Pharoah Sanders — The Creator Has a Master Plan", 0, 30),
            (PipelineStage.ANALYZING, None, 30, 55),
            (PipelineStage.FETCHING_ARTWORK, None, 55, 60),
            (PipelineStage.TAGGING, None, 60, 65),
            (PipelineStage.SEPARATING_STEMS, None, 65, 98),
        ]
        step = 0
        def tick():
            nonlocal step
            if step >= 100:
                row1.apply_event(QueueEvent(
                    type=QueueEventType.JOB_COMPLETED,
                    job_id=1,
                    display_name="Pharoah Sanders — The Creator Has a Master Plan",
                    track_id=42,
                    bpm=94.5, musical_key="Am", camelot_key="8A",
                    overall_percent=100.0,
                ))
                return
            # Find stage for this step
            current_stage = PipelineStage.DOWNLOADING
            for stg, name, lo, hi in stages:
                if lo <= step < hi:
                    current_stage = stg
                    break
            bpm = 94.5 if step > 55 else None
            key = "Am" if step > 58 else None
            cam = "8A" if step > 58 else None
            row1.apply_event(QueueEvent(
                type=QueueEventType.JOB_PROGRESS,
                job_id=1,
                display_name="Pharoah Sanders — The Creator Has a Master Plan",
                stage=current_stage,
                overall_percent=float(step),
                bpm=bpm, musical_key=key, camelot_key=cam,
                message=current_stage.value,
            ))
            step += 2
            root.after(200, tick)
        tick()

    root.after(1500, simulate_progress)

    # Simulate a failure on row2
    def fail_row2():
        row2.apply_event(QueueEvent(
            type=QueueEventType.JOB_PROGRESS,
            job_id=2, stage=PipelineStage.DOWNLOADING,
            overall_percent=23.0, message="Downloading",
        ))
        root.after(2500, lambda: row2.apply_event(QueueEvent(
            type=QueueEventType.JOB_FAILED,
            job_id=2,
            error_message="Private video — authentication required",
            overall_percent=23.0,
        )))
    root.after(500, fail_row2)

    # Section: virtualized data table with 50,000 rows
    ctk.CTkLabel(
        content, text="Virtualized Data Table (50,000 rows)",
        **style_label_heading(theme),
    ).pack(anchor="w", padx=theme.space.xl, pady=(0, theme.space.md))

    ctk.CTkLabel(
        content,
        text="Scroll, click headers to sort, Ctrl/Shift-click for multi-select.",
        **style_label_meta(theme),
    ).pack(anchor="w", padx=theme.space.xl, pady=(0, theme.space.sm))

    columns = [
        Column("artist", "Artist", width=220),
        Column("title", "Title", width=280),
        Column("genre", "Genre", width=120),
        Column("bpm", "BPM", width=80, numeric=True,
               renderer=lambda r: f"{r['bpm']:.1f}"),
        Column("camelot_key", "Key", width=70, numeric=True),
        Column("year", "Year", width=70, numeric=True),
    ]

    # Synthesize 50k rows
    artists = ["João Gilberto", "Pharoah Sanders", "Alice Coltrane",
               "Idris Muhammad", "Gal Costa", "Milton Nascimento",
               "Roy Ayers", "Hermeto Pascoal", "Sun Ra", "Dorothy Ashby"]
    titles = ["Corcovado", "Harvest Time", "Turiya and Ramakrishna",
              "Could Heaven Ever Be Like This", "Baby", "Travessia",
              "Everybody Loves the Sunshine", "Slaves Mass",
              "Space Is the Place", "Afro-Harping"]
    genres = ["Jazz", "Bossa Nova", "Soul", "Fusion", "MPB", "Spiritual Jazz"]
    keys = [f"{n}{r}" for n in range(1, 13) for r in "AB"]

    rng = random.Random(42)
    rows = []
    for i in range(50_000):
        rows.append({
            "artist": f"{rng.choice(artists)} #{i}",
            "title": rng.choice(titles),
            "genre": rng.choice(genres),
            "bpm": rng.uniform(70, 160),
            "camelot_key": rng.choice(keys),
            "year": rng.randint(1965, 2024),
        })

    print(f"[data_table] Generated {len(rows):,} rows")
    t_construct = time.monotonic()

    table = VirtualDataTable(
        content, theme, columns,
        on_sort_changed=lambda s: print(f"sort: {s.column_key} {s.direction.value}"),
        on_row_activated=lambda r: toast_layer.show(
            f"Opened: {r['artist']} — {r['title']}", kind="info",
        ),
        on_selection_changed=lambda rows: print(f"selected {len(rows)} row(s)"),
    )
    table.pack(fill="both", expand=True,
               padx=theme.space.xl, pady=(0, theme.space.xl))
    # Fix table height so it fits in the scrollable demo window
    table.configure(height=500)

    t_set = time.monotonic()
    table.set_data(rows)
    t_done = time.monotonic()
    print(f"[data_table] Construct: {(t_set - t_construct)*1000:.1f}ms  "
          f"set_data(50k): {(t_done - t_set)*1000:.1f}ms")

    # Handle sorts
    def on_sort(s):
        if s.direction == SortDirection.NONE:
            table.set_data(rows)
            return
        reverse = s.direction == SortDirection.DESC
        sorted_rows = sorted(rows, key=lambda r: r.get(s.column_key) or "",
                             reverse=reverse)
        table.set_data(sorted_rows)
        table.set_sort(s.column_key, s.direction)

    table.on_sort_changed = on_sort

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
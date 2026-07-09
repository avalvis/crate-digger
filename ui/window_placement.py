"""
ui/window_placement.py
──────────────────────────────────────────────────────────────────────
Reliable CTk root placement — avoids the broken flash / off-center
maximize that happens when geometry and state('zoomed') fight on Windows.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    import customtkinter as ctk

_log = logging.getLogger("cratedigger.ui.window_placement")


@dataclass(frozen=True, slots=True)
class WorkArea:
    x: int
    y: int
    width: int
    height: int


def prepare_ctk_environment() -> None:
    """
    Call once before constructing the main CTk() root.
    Disables CTk DPI rescaling and the Windows titlebar withdraw/deiconify
    dance that breaks maximized placement on focus.
    """
    try:
        import customtkinter as ctk
    except Exception:
        return

    if hasattr(ctk, "deactivate_automatic_dpi_awareness"):
        try:
            ctk.deactivate_automatic_dpi_awareness()
        except Exception:
            _log.debug("deactivate_automatic_dpi_awareness failed", exc_info=True)

    try:
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            ctk.CTk._deactivate_windows_window_header_manipulation = True
        except Exception:
            pass


def get_work_area(root: "ctk.CTk") -> WorkArea:
    """Return the monitor work area (excludes taskbar on Windows)."""
    if sys.platform == "win32":
        try:
            import ctypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_long),
                    ("rcMonitor", _RECT),
                    ("rcWork", _RECT),
                    ("dwFlags", ctypes.c_long),
                ]

            monitor_info = _MONITORINFO()
            monitor_info.cbSize = ctypes.sizeof(_MONITORINFO)
            monitor = None
            try:
                hwnd = root.winfo_id()
                if hwnd:
                    monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
            except Exception:
                monitor = None
            if not monitor:
                monitor = ctypes.windll.user32.MonitorFromPoint(0, 2)
            if ctypes.windll.user32.GetMonitorInfoW(
                monitor, ctypes.byref(monitor_info),
            ):
                work = monitor_info.rcWork
                return WorkArea(
                    int(work.left),
                    int(work.top),
                    int(work.right - work.left),
                    int(work.bottom - work.top),
                )
        except Exception:
            _log.debug("GetMonitorInfoW failed", exc_info=True)

        try:
            import ctypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = _RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0,
            ):
                return WorkArea(
                    int(rect.left),
                    int(rect.top),
                    int(rect.right - rect.left),
                    int(rect.bottom - rect.top),
                )
        except Exception:
            _log.debug("SPI_GETWORKAREA failed", exc_info=True)

    try:
        return WorkArea(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    except Exception:
        return WorkArea(0, 0, 1280, 800)


def sync_ctk_window_metrics(
    root: "ctk.CTk",
    *,
    min_width: int,
    min_height: int,
) -> None:
    """Align CTk's logical size with the real window dimensions."""
    try:
        root.update_idletasks()
        w = root.winfo_width()
        h = root.winfo_height()
        if w < 200 or h < 200:
            return
        root._current_width = max(min_width, root._reverse_window_scaling(w))
        root._current_height = max(min_height, root._reverse_window_scaling(h))
        root.minsize(min_width, min_height)
        root.maxsize(1_000_000, 1_000_000)
    except Exception:
        _log.debug("Could not sync CTk window metrics", exc_info=True)


def patch_ctk_scaling_guard(
    root: "ctk.CTk",
    *,
    is_maximized: Callable[[], bool],
) -> None:
    """
    CTk's _set_scaling() forces geometry to _current_width x _current_height.
    When those lag behind the real window (common after maximize), the first
    click snaps the window to a stale size/position. Sync before scaling updates.
    """
    if getattr(root, "_cratedigger_scaling_patched", False):
        return

    original = root._set_scaling

    def patched_set_scaling(new_widget_scaling, new_window_scaling) -> None:
        if is_maximized():
            sync_ctk_window_metrics(
                root,
                min_width=getattr(root, "_cratedigger_min_width", 1120),
                min_height=getattr(root, "_cratedigger_min_height", 720),
            )
        original(new_widget_scaling, new_window_scaling)

    root._set_scaling = patched_set_scaling  # type: ignore[method-assign]
    root._cratedigger_scaling_patched = True


def apply_work_area_geometry(
    root: "ctk.CTk",
    *,
    min_width: int,
    min_height: int,
    reveal: bool = False,
) -> None:
    """
    Fill the monitor work area using CTk.geometry() — not wm state zoom.

    CTk tracks its own logical size; Windows' zoomed state leaves that stale
    and the window jumps on the first interaction. Setting geometry through CTk
    keeps everything consistent.
    """
    try:
        root.update_idletasks()
        area = get_work_area(root)
        lw = max(min_width, root._reverse_window_scaling(area.width))
        lh = max(min_height, root._reverse_window_scaling(area.height))
        root.geometry(f"{lw}x{lh}+{area.x}+{area.y}")
        root.update_idletasks()
        sync_ctk_window_metrics(root, min_width=min_width, min_height=min_height)
        if reveal:
            try:
                root.deiconify()
            except Exception:
                pass
        root.update_idletasks()
    except Exception:
        _log.exception("Could not apply work-area geometry")


def is_effectively_maximized(
    root: "ctk.CTk",
    *,
    fallback: bool = True,
    tolerance: int = 12,
) -> bool:
    """True when the window fills the monitor work area (geometry-based maximize)."""
    try:
        area = get_work_area(root)
        return (
            abs(root.winfo_width() - area.width) <= tolerance
            and abs(root.winfo_height() - area.height) <= tolerance
            and abs(root.winfo_x() - area.x) <= tolerance
            and abs(root.winfo_y() - area.y) <= tolerance
        )
    except Exception:
        return fallback


def show_root_window(
    root: "ctk.CTk",
    *,
    maximized: bool,
    width: int,
    height: int,
    min_width: int,
    min_height: int,
) -> None:
    """
    Reveal a withdrawn CTk root at the correct size and position.
    Must be called after the widget tree is built and update_idletasks() has run.
    """
    root._cratedigger_min_width = min_width  # type: ignore[attr-defined]
    root._cratedigger_min_height = min_height  # type: ignore[attr-defined]

    root.update_idletasks()
    area = get_work_area(root)

    if maximized:
        apply_work_area_geometry(
            root,
            min_width=min_width,
            min_height=min_height,
            reveal=True,
        )
    else:
        w = max(min_width, min(width, area.width))
        h = max(min_height, min(height, area.height))
        x = area.x + max(0, (area.width - w) // 2)
        y = area.y + max(0, (area.height - h) // 2)
        try:
            root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            _log.debug("geometry failed", exc_info=True)
        sync_ctk_window_metrics(root, min_width=min_width, min_height=min_height)
        try:
            root.deiconify()
        except Exception:
            pass

    root.update_idletasks()
    root.lift()
    try:
        root.focus_force()
    except Exception:
        pass


def apply_window_icon(root: "ctk.CTk", icon_path: Optional[str]) -> None:
    if not icon_path:
        return
    try:
        from pathlib import Path
        p = Path(icon_path)
        if p.is_file():
            root.iconbitmap(str(p))
            root._iconbitmap_method_called = True  # type: ignore[attr-defined]
    except Exception:
        _log.debug("Could not set window icon from %s", icon_path, exc_info=True)

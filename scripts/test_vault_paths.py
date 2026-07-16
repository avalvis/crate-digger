"""Focused tests for user-facing Vault folder layouts."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from core.pipeline import IngestionPipeline
from utils.config import GeneralConfig
from utils.paths import build_vault_track_dir


def test_default_layout_groups_tracks_by_rip_date() -> None:
    path = build_vault_track_dir(
        Path("vault"),
        genre="Soul",
        bpm=94.0,
        camelot_key="8A",
        artist="Marlena Shaw",
        title="California Soul",
        filed_on=date(2026, 7, 16),
    )

    assert path == Path("vault/2026-07-16/Marlena Shaw_California Soul")


def test_genre_layout_remains_available() -> None:
    path = build_vault_track_dir(
        Path("vault"),
        genre="Soul",
        bpm=94.0,
        camelot_key="8A",
        artist="Marlena Shaw",
        title="California Soul",
        scheme="genre/bpm_key_artist_title",
        filed_on=date(2026, 7, 16),
    )

    assert path == Path("vault/Soul/94_8A_Marlena Shaw_California Soul")


def test_pipeline_folder_scheme_can_change_without_restart() -> None:
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline._folder_scheme = "genre/bpm_key_artist_title"

    pipeline.update_folder_scheme("date/artist_title")

    assert pipeline._folder_scheme == "date/artist_title"


def test_fresh_config_uses_recent_first_layout() -> None:
    assert GeneralConfig().vault_folder_scheme == "date/artist_title"

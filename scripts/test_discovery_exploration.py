"""Unit tests for discovery exploration / variety behavior."""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import VaultDatabase
from core.discovery import (
    DiscogsCandidate,
    DiscoveryEngine,
    DiscoveryFilters,
)


def _candidate(
    mid: int,
    *,
    artist: str | None = None,
    title: str | None = None,
    year: int | None = 1975,
    country: str | None = "Greece",
    genres: tuple[str, ...] = ("Folk, World, & Country",),
    styles: tuple[str, ...] = ("Laïkó",),
    formats: tuple[str, ...] = ("Vinyl", "LP"),
    have: int = 100,
    want: int = 50,
) -> DiscogsCandidate:
    return DiscogsCandidate(
        master_id=mid,
        release_id=mid + 1000,
        artist=artist or f"Artist {mid}",
        title=title or f"Title {mid}",
        year=year,
        country=country,
        genres=genres,
        styles=styles,
        formats=formats,
        have=have,
        want=want,
    )


def _engine(db: VaultDatabase, seed: int = 42) -> DiscoveryEngine:
    eng = DiscoveryEngine(
        db=db,
        discogs_token="test-token",
        rng=random.Random(seed),
    )
    eng._discogs_get = MagicMock()  # type: ignore[method-assign]
    return eng


def test_effective_min_have_relaxed_for_country() -> None:
    filters = DiscoveryFilters(country="Greece", min_have=50)
    assert DiscoveryEngine._effective_min_have(filters) == 15


def test_effective_min_have_unchanged_wide_open() -> None:
    filters = DiscoveryFilters(min_have=50)
    assert DiscoveryEngine._effective_min_have(filters) == 50


def test_session_surfaced_excluded_from_pool() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            candidates = [_candidate(i) for i in range(1, 21)]
            eng._remember_surfaced([1, 2, 3])
            pool = eng._rank_and_shuffle(candidates, DiscoveryFilters())
            ids = [c.master_id for c in pool]
            assert 1 not in ids and 2 not in ids and 3 not in ids
        finally:
            db.close()


def test_jitter_produces_different_order() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            candidates = [_candidate(i, have=50 + i, want=30 + i) for i in range(1, 41)]
            orders: set[tuple[int, ...]] = set()
            for seed in range(5):
                eng = _engine(db, seed=seed)
                pool = eng._rank_and_shuffle(candidates, DiscoveryFilters())
                orders.add(tuple(c.master_id for c in pool[:12]))
            assert len(orders) > 1
        finally:
            db.close()


def test_strict_filters_reject_known_mismatches() -> None:
    cand = _candidate(
        77,
        year=1974,
        country="US",
        genres=("Jazz",),
        styles=("Soul-Jazz",),
        formats=("Vinyl", "LP", "Album"),
    )
    assert DiscoveryEngine._matches_strict_filters(
        cand,
        DiscoveryFilters(
            year_min=1970,
            year_max=1979,
            country="USA",
            genre="Jazz",
            style="Soul-Jazz",
            format="LP",
        ),
    )
    assert not DiscoveryEngine._matches_strict_filters(
        cand, DiscoveryFilters(country="Brazil"),
    )
    assert not DiscoveryEngine._matches_strict_filters(
        cand, DiscoveryFilters(genre="Funk / Soul"),
    )
    assert not DiscoveryEngine._matches_strict_filters(
        cand, DiscoveryFilters(style="Spiritual Jazz"),
    )
    assert not DiscoveryEngine._matches_strict_filters(
        cand, DiscoveryFilters(format="CD"),
    )
    assert not DiscoveryEngine._matches_strict_filters(
        cand, DiscoveryFilters(year_min=1980, year_max=1985),
    )


def test_strict_filters_allow_unknown_metadata() -> None:
    cand = _candidate(
        78,
        year=None,
        country=None,
        genres=(),
        styles=(),
        formats=(),
    )
    assert DiscoveryEngine._matches_strict_filters(
        cand,
        DiscoveryFilters(
            year_min=1970,
            year_max=1979,
            country="Brazil",
            genre="Jazz",
            style="Soul-Jazz",
            format="Vinyl",
        ),
    )


def test_rank_interleaves_producer_lanes() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db, seed=9)
            candidates = [
                _candidate(1, country="USA", genres=("Funk / Soul",), styles=("Funk",)),
                _candidate(2, country="USA", genres=("Jazz",), styles=("Jazz-Funk",)),
                _candidate(3, country="Italy", genres=("Stage & Screen",), styles=("Library Music",)),
                _candidate(4, country="Brazil", genres=("Latin",), styles=("MPB",)),
                _candidate(5, country="Nigeria", genres=("Folk, World, & Country",), styles=("Afrobeat",)),
                _candidate(6, country="Greece", genres=("Folk, World, & Country",), styles=("Laïkó",)),
                _candidate(7, country="Germany", genres=("Rock",), styles=("Krautrock",)),
                _candidate(8, country="USA", genres=("Electronic",), styles=("Ambient",)),
            ]
            pool = eng._rank_and_shuffle(candidates, DiscoveryFilters())
            lanes = {eng._producer_lane(c) for c in pool[:7]}
            assert len(lanes) >= 6
        finally:
            db.close()


def test_ytm_scoring_accepts_strong_original_match() -> None:
    cand = _candidate(
        90,
        artist="Pharoah Sanders",
        title="The Creator Has A Master Plan",
        country="USA",
        genres=("Jazz",),
        styles=("Spiritual Jazz",),
    )
    scored = DiscoveryEngine._score_ytm_results(
        [{
            "videoId": "ok",
            "title": "Pharoah Sanders - The Creator Has A Master Plan",
            "artists": [{"name": "Pharoah Sanders"}],
            "album": {"name": "Karma"},
        }],
        cand,
        "Pharoah Sanders",
    )
    assert scored and scored[0][0] > 0.75


def test_ytm_scoring_rejects_noise_and_weak_title_only() -> None:
    cand = _candidate(
        91,
        artist="Pharoah Sanders",
        title="The Creator Has A Master Plan",
        country="USA",
        genres=("Jazz",),
        styles=("Spiritual Jazz",),
    )
    results = [
        {
            "videoId": "live",
            "title": "Pharoah Sanders - The Creator Has A Master Plan Live",
            "artists": [{"name": "Pharoah Sanders"}],
        },
        {
            "videoId": "karaoke",
            "title": "The Creator Has A Master Plan Karaoke",
            "artists": [{"name": "Pharoah Sanders"}],
        },
        {
            "videoId": "weak",
            "title": "The Creator Has A Master Plan",
            "artists": [],
        },
        {
            "videoId": "wrong",
            "title": "Completely Different Song",
            "artists": [{"name": "Someone Else"}],
        },
    ]
    assert DiscoveryEngine._score_ytm_results(results, cand, "Pharoah Sanders") == []


def test_ytm_scoring_prefers_official_catalog_audio() -> None:
    cand = _candidate(92, artist="Roy Ayers Ubiquity", title="Everybody Loves The Sunshine")
    results = [
        {
            "videoId": "popular_reupload",
            "title": "Roy Ayers - Everybody Loves The Sunshine",
            "artists": [{"name": "RareGrooveUploads"}],
            "views": "80M views",
            "resultType": "video",
        },
        {
            "videoId": "official_audio",
            "title": "Everybody Loves The Sunshine",
            "artists": [{"name": "Roy Ayers Ubiquity"}],
            "views": "2M views",
            "resultType": "song",
            "videoType": "MUSIC_VIDEO_TYPE_ATV",
        },
    ]
    scored = DiscoveryEngine._score_ytm_results(
        results, cand, "Roy Ayers Ubiquity",
    )
    assert scored[0][1]["videoId"] == "official_audio"


def test_ytm_scoring_uses_popularity_between_equal_uploads() -> None:
    cand = _candidate(93, artist="Dorothy Ashby", title="Soul Vibrations")
    results = [
        {
            "videoId": "small",
            "title": "Dorothy Ashby - Soul Vibrations",
            "artists": [{"name": "Dorothy Ashby"}],
            "views": "12K views",
        },
        {
            "videoId": "popular",
            "title": "Dorothy Ashby - Soul Vibrations",
            "artists": [{"name": "Dorothy Ashby"}],
            "views": "4.5M views",
        },
    ]
    scored = DiscoveryEngine._score_ytm_results(results, cand, "Dorothy Ashby")
    assert scored[0][1]["videoId"] == "popular"


def test_ytm_scoring_rejects_bad_recording_variants() -> None:
    cand = _candidate(94, artist="Cymande", title="Dove")
    results = [
        {
            "videoId": "rehearsal",
            "title": "Cymande - Dove Rehearsal",
            "artists": [{"name": "Cymande"}],
            "views": "8M views",
        },
        {
            "videoId": "slowed",
            "title": "Cymande - Dove Slowed",
            "artists": [{"name": "Cymande"}],
            "views": "20M views",
        },
    ]
    assert DiscoveryEngine._score_ytm_results(results, cand, "Cymande") == []


def test_ytm_match_compares_search_buckets_before_choosing() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            eng._ytm_client = object()
            cand = _candidate(
                95, artist="Bobbi Humphrey", title="Harlem River Drive",
            )

            def _fake_search(_ytm: object, _query: str, ytm_filter: str) -> list[dict]:
                if ytm_filter == "songs":
                    return [{
                        "videoId": "acceptable",
                        "title": "Bobbi Humphrey - Harlem River Drive",
                        "artists": [{"name": "Bobbi Humphrey"}],
                        "resultType": "video",
                        "views": "15K views",
                    }]
                return [{
                    "videoId": "official",
                    "title": "Harlem River Drive",
                    "artists": [{"name": "Bobbi Humphrey"}],
                    "resultType": "song",
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "views": "900K views",
                }]

            eng._ytm_search = _fake_search  # type: ignore[method-assign]
            suggestion = eng._match_youtube(cand)
            assert suggestion.youtube_video_id == "official"
        finally:
            db.close()


def test_search_fetches_shuffled_pages_until_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            pages_called: list[int] = []
            years_called: list[int] = []

            def _fake_discogs_get(_endpoint: str, params: dict) -> dict:
                page = int(params["page"])
                selected_year = int(params["year"])
                pages_called.append(page)
                years_called.append(selected_year)
                results = []
                for i in range(50):
                    mid = selected_year * 1000 + i
                    results.append({
                        "master_id": mid,
                        "id": mid + 1,
                        "title": f"Artist {mid} - Album {mid}",
                        "year": selected_year,
                        "country": "Greece",
                        "genre": ["Folk, World, & Country"],
                        "style": ["Laïkó"],
                        "format": ["Vinyl", "LP"],
                        "community": {"have": 80, "want": 40},
                    })
                return {
                    "results": results,
                    "pagination": {"pages": 10, "page": page, "per_page": 50},
                }

            eng._discogs_get = _fake_discogs_get  # type: ignore[method-assign]
            filters = DiscoveryFilters(
                country="Greece",
                year_min=1970,
                year_max=1985,
                min_have=50,
            )
            out = eng._search_discogs(filters)
            assert len(out) >= DiscoveryEngine.TARGET_POOL_SIZE
            assert set(pages_called) == {1}
            assert len(set(years_called)) >= 3
            assert all(1970 <= year <= 1985 for year in years_called)
            assert len(pages_called) <= DiscoveryEngine.MAX_SEARCH_PAGES_NARROW
        finally:
            db.close()


def test_search_respects_page_budget_when_no_candidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            pages_called: list[int] = []
            years_called: list[int] = []

            def _fake_discogs_get(_endpoint: str, params: dict) -> dict:
                page = int(params["page"])
                pages_called.append(page)
                years_called.append(int(params["year"]))
                return {
                    "results": [{
                        "master_id": page,
                        "id": page + 1,
                        "title": f"Artist {page} - Album {page}",
                        "year": 1900,  # filtered out by the selected era
                        "country": "Greece",
                        "genre": ["Rock"],
                        "style": [],
                        "format": ["Vinyl", "LP"],
                        "community": {"have": 80, "want": 40},
                    }],
                    "pagination": {"pages": 200, "page": page, "per_page": 50},
                }

            eng._discogs_get = _fake_discogs_get  # type: ignore[method-assign]
            filters = DiscoveryFilters(
                country="Greece",
                year_min=1930,
                year_max=1979,
                min_have=50,
            )
            out = eng._search_discogs(filters)
            assert out == []
            assert len(pages_called) == DiscoveryEngine.MAX_SEARCH_PAGES_NARROW
            assert min(years_called) < 1940
            assert max(years_called) >= 1970
        finally:
            db.close()


def main() -> None:
    test_effective_min_have_relaxed_for_country()
    test_effective_min_have_unchanged_wide_open()
    test_session_surfaced_excluded_from_pool()
    test_jitter_produces_different_order()
    test_strict_filters_reject_known_mismatches()
    test_strict_filters_allow_unknown_metadata()
    test_rank_interleaves_producer_lanes()
    test_ytm_scoring_accepts_strong_original_match()
    test_ytm_scoring_rejects_noise_and_weak_title_only()
    test_search_fetches_shuffled_pages_until_target()
    test_search_respects_page_budget_when_no_candidates()
    print("test_discovery_exploration: all passed")


if __name__ == "__main__":
    main()

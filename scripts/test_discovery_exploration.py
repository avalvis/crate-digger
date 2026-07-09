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


def _candidate(mid: int, *, have: int = 100, want: int = 50) -> DiscogsCandidate:
    return DiscogsCandidate(
        master_id=mid,
        release_id=mid + 1000,
        artist=f"Artist {mid}",
        title=f"Title {mid}",
        year=1975,
        country="Greece",
        genres=("Folk, World, & Country",),
        styles=("Laïkó",),
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


def test_search_fetches_shuffled_pages_until_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            pages_called: list[int] = []

            def _fake_discogs_get(_endpoint: str, params: dict) -> dict:
                page = int(params["page"])
                pages_called.append(page)
                results = []
                for i in range(50):
                    mid = page * 1000 + i
                    results.append({
                        "master_id": mid,
                        "id": mid + 1,
                        "title": f"Artist {mid} - Album {mid}",
                        "year": 1975,
                        "country": "Greece",
                        "genre": ["Folk, World, & Country"],
                        "style": ["Laïkó"],
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
            assert 1 in pages_called
            assert len(set(pages_called)) >= 3
            assert len(pages_called) <= DiscoveryEngine.MAX_SEARCH_PAGES_NARROW
        finally:
            db.close()


def test_search_respects_page_budget_when_no_candidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = VaultDatabase(Path(td) / "vault.db")
        try:
            eng = _engine(db)
            pages_called: list[int] = []

            def _fake_discogs_get(_endpoint: str, params: dict) -> dict:
                page = int(params["page"])
                pages_called.append(page)
                return {
                    "results": [{
                        "master_id": page,
                        "id": page + 1,
                        "title": f"Artist {page} - Album {page}",
                        "year": 1960,  # filtered out by 1970-1985 range
                        "country": "Greece",
                        "genre": ["Rock"],
                        "style": [],
                        "community": {"have": 80, "want": 40},
                    }],
                    "pagination": {"pages": 200, "page": page, "per_page": 50},
                }

            eng._discogs_get = _fake_discogs_get  # type: ignore[method-assign]
            filters = DiscoveryFilters(
                country="Greece",
                year_min=1970,
                year_max=1985,
                min_have=50,
            )
            out = eng._search_discogs(filters)
            assert out == []
            assert len(pages_called) <= DiscoveryEngine.MAX_SEARCH_PAGES_NARROW
        finally:
            db.close()


def main() -> None:
    test_effective_min_have_relaxed_for_country()
    test_effective_min_have_unchanged_wide_open()
    test_session_surfaced_excluded_from_pool()
    test_jitter_produces_different_order()
    test_search_fetches_shuffled_pages_until_target()
    test_search_respects_page_budget_when_no_candidates()
    print("test_discovery_exploration: all passed")


if __name__ == "__main__":
    main()

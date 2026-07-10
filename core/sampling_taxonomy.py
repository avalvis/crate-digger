"""
core/sampling_taxonomy.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Sample-Friendliness Taxonomy

Encodes what boom-bap / lo-fi / MPC producers actually dig for, as a set
of weight tables over Discogs genres, styles, countries, and eras. The
discovery engine multiplies a candidate's raw Discogs desirability
(want/have ratio) by a "sample score" derived here so the roulette
tilts toward sample-friendly gems.

Design principle: **weight, never exclude.** Every candidate keeps a
non-zero score, so the Dig button can always surface a wildcard. We only
change the *odds*, not the *possibility*.

The tiers are grounded in crate-digging tradition:
  • 1960s-70s soul / funk / jazz are the bedrock of hip-hop sampling.
  • Library music, soundtracks, gospel, psych, and international grooves
    (Brazilian, African, Latin) are the deep-dig staples.
  • Greek rebetiko / laïkó / éntekhno and the Greek 60s-70s psych/funk
    scene are surfaced deliberately here — obscure, modal, and rich with
    breaks, per the user's crate.

Nothing in this module touches the network, the DB, or the UI. It's a
pure scoring helper: give it genres/styles/country/year, get a float.
"""

from __future__ import annotations

import random
from typing import Iterable, Optional

# ─── Weight tiers ────────────────────────────────────────────────────
# Multipliers applied to the base desirability. 1.0 is neutral. Values
# above 1.0 boost; below 1.0 gently deprioritize. Nothing goes to 0.

_TIER_PRIME = 1.75     # the classic sampling bedrock
_TIER_STRONG = 1.45    # deep-dig staples
_TIER_GOOD = 1.2       # frequently flipped
_TIER_NEUTRAL = 1.0    # no opinion
_TIER_SOFT = 0.85      # rarely sampled, mild deprioritize
_TIER_COLD = 0.62      # modern/electronic-leaning, least sample-friendly

# Country bonus — additive nudge folded in multiplicatively below.
_GREECE_BONUS = 1.35   # the user's home crate; surface Greek gems more often
_INTL_DIG_BONUS = 1.2   # classic international digging territories


# ─── Genre weights (Discogs top-level genre strings) ─────────────────

_GENRE_WEIGHTS: dict[str, float] = {
    "funk / soul": _TIER_PRIME,
    "jazz": _TIER_PRIME,
    "latin": _TIER_STRONG,
    "stage & screen": _TIER_STRONG,   # soundtracks / library / OST
    "reggae": _TIER_GOOD,
    "folk, world, & country": _TIER_GOOD,
    "blues": _TIER_GOOD,
    "rock": _TIER_NEUTRAL,            # psych/prog subgenres boosted via style
    "classical": _TIER_SOFT,
    "brass & military": _TIER_SOFT,
    "pop": _TIER_SOFT,
    "electronic": _TIER_COLD,         # trip-hop/downtempo boosted via style
    "hip hop": _TIER_SOFT,            # usually the destination, not the source
    "children's": _TIER_SOFT,
    "non-music": _TIER_SOFT,
}


# ─── Style weights (Discogs style strings) ───────────────────────────
# Styles override/augment the genre signal — a "Soul-Jazz" Jazz record
# should rank above a generic jazz record.

_STYLE_WEIGHTS: dict[str, float] = {
    # Soul / funk core
    "soul": _TIER_PRIME,
    "funk": _TIER_PRIME,
    "rhythm & blues": _TIER_PRIME,
    "breaks": _TIER_PRIME,
    "breakbeat": _TIER_STRONG,
    "neo soul": _TIER_STRONG,
    "disco": _TIER_STRONG,
    "boogie": _TIER_STRONG,
    "gospel": _TIER_PRIME,
    "p.funk": _TIER_PRIME,
    # Jazz family — the deep well
    "soul-jazz": _TIER_PRIME,
    "jazz-funk": _TIER_PRIME,
    "spiritual jazz": _TIER_PRIME,
    "hard bop": _TIER_STRONG,
    "fusion": _TIER_STRONG,
    "free jazz": _TIER_GOOD,
    "latin jazz": _TIER_STRONG,
    "acid jazz": _TIER_STRONG,
    "bossa nova": _TIER_STRONG,
    "modal": _TIER_STRONG,
    "big band": _TIER_GOOD,
    "swing": _TIER_GOOD,
    "lounge": _TIER_STRONG,
    "easy listening": _TIER_GOOD,
    # Library / soundtrack / cinematic
    "library music": _TIER_PRIME,
    "score": _TIER_PRIME,
    "soundtrack": _TIER_PRIME,
    "theme": _TIER_GOOD,
    # Brazilian / Latin
    "mpb": _TIER_STRONG,
    "samba": _TIER_STRONG,
    "tropicália": _TIER_STRONG,
    "tropicalia": _TIER_STRONG,
    "boogaloo": _TIER_STRONG,
    "salsa": _TIER_GOOD,
    "bolero": _TIER_GOOD,
    "cumbia": _TIER_GOOD,
    "pachanga": _TIER_GOOD,
    "mambo": _TIER_GOOD,
    "descarga": _TIER_STRONG,
    # African
    "afrobeat": _TIER_STRONG,
    "highlife": _TIER_STRONG,
    "ethio-jazz": _TIER_PRIME,
    "afro-funk": _TIER_PRIME,
    "juju": _TIER_GOOD,
    # Psych / prog / rock textures worth flipping
    "psychedelic rock": _TIER_STRONG,
    "prog rock": _TIER_STRONG,
    "krautrock": _TIER_STRONG,
    "folk rock": _TIER_GOOD,
    "acid rock": _TIER_GOOD,
    "space rock": _TIER_GOOD,
    "experimental": _TIER_GOOD,
    "avantgarde": _TIER_GOOD,
    # Reggae / dub
    "dub": _TIER_STRONG,
    "roots reggae": _TIER_STRONG,
    "rocksteady": _TIER_STRONG,
    "ska": _TIER_GOOD,
    "dancehall": _TIER_SOFT,
    # Downtempo / trip-hop-adjacent electronic (sample-culture kin)
    "trip hop": _TIER_STRONG,
    "downtempo": _TIER_STRONG,
    "ambient": _TIER_GOOD,
    "lo-fi": _TIER_STRONG,
    "abstract": _TIER_GOOD,
    "instrumental": _TIER_GOOD,
    # Greek crate — deliberately surfaced
    "rebetiko": _TIER_PRIME,
    "rebetico": _TIER_PRIME,
    "laïkó": _TIER_PRIME,
    "laiko": _TIER_PRIME,
    "éntekhno": _TIER_PRIME,
    "entekhno": _TIER_PRIME,
    "laïko-éntekhno": _TIER_PRIME,
    "smyrneika": _TIER_STRONG,
    "nisiotika": _TIER_GOOD,
    "dimotiká": _TIER_GOOD,
    "dimotika": _TIER_GOOD,
    "éntekhno laïkó": _TIER_PRIME,
    # Cold-leaning modern electronic dance styles
    "techno": _TIER_COLD,
    "house": _TIER_COLD,
    "trance": _TIER_COLD,
    "eurodance": _TIER_COLD,
    "gabber": _TIER_COLD,
    "hardcore": _TIER_COLD,
    "jungle": _TIER_SOFT,
    "deep house": _TIER_COLD,
    "garage house": _TIER_COLD,
    "synth-pop": _TIER_SOFT,
    "euro house": _TIER_COLD,
}


# ─── Greek-signal detection ──────────────────────────────────────────
# Styles that imply the record is Greek even when the country field is
# blank/ambiguous.
_GREEK_STYLE_SIGNALS: frozenset[str] = frozenset({
    "rebetiko", "rebetico", "laïkó", "laiko", "éntekhno", "entekhno",
    "laïko-éntekhno", "smyrneika", "nisiotika", "dimotiká", "dimotika",
    "éntekhno laïkó", "laïka",
})

# Classic international digging countries (beyond USA/UK) get a nudge.
_INTL_DIG_COUNTRIES: frozenset[str] = frozenset({
    "brazil", "nigeria", "ghana", "ethiopia", "japan", "france", "italy",
    "germany", "turkey", "colombia", "cuba", "mexico", "peru", "jamaica",
    "south africa", "argentina", "spain", "poland", "yugoslavia", "ussr",
    "indonesia", "thailand", "india", "algeria", "morocco",
})


# ─── Era weighting ───────────────────────────────────────────────────
# Peak sampling era is the late-60s through the 70s. We taper gently on
# both sides so a 1982 boogie record still ranks well and a 1958 hard-bop
# side isn't buried. Missing years get a neutral-ish default so unknown
# gems aren't punished into oblivion.

def era_weight(year: Optional[int]) -> float:
    """Return a 0.7..1.6 multiplier reflecting how sample-rich an era is."""
    if year is None:
        return 0.95  # unknown — treat as slightly-below-neutral, not excluded
    if 1965 <= year <= 1979:
        return 1.6   # the golden window
    if 1960 <= year <= 1984:
        return 1.4   # broad classic era
    if 1950 <= year <= 1989:
        return 1.2   # still fertile (early rock'n'roll, 80s boogie, etc.)
    if 1990 <= year <= 1999:
        return 1.0   # 90s — usable but more picked-over
    if year < 1950:
        return 1.05  # pre-war blues/jazz — niche but distinctive
    return 0.85      # 2000s+ — least likely to be a hidden break


def _norm(s: str) -> str:
    return s.strip().lower()


def _best_style_weight(styles: Iterable[str]) -> Optional[float]:
    """Highest style multiplier among a record's styles, or None."""
    best: Optional[float] = None
    for s in styles:
        w = _STYLE_WEIGHTS.get(_norm(s))
        if w is not None and (best is None or w > best):
            best = w
    return best


def _best_genre_weight(genres: Iterable[str]) -> Optional[float]:
    best: Optional[float] = None
    for g in genres:
        w = _GENRE_WEIGHTS.get(_norm(g))
        if w is not None and (best is None or w > best):
            best = w
    return best


def is_greek(styles: Iterable[str], country: Optional[str]) -> bool:
    """Heuristic: does this record read as Greek?"""
    if country and _norm(country) == "greece":
        return True
    return any(_norm(s) in _GREEK_STYLE_SIGNALS for s in styles)


def sample_affinity(
    *,
    genres: Iterable[str],
    styles: Iterable[str],
    country: Optional[str],
    year: Optional[int],
) -> float:
    """
    Compute a sample-friendliness multiplier for a candidate.

    Combines the best matching genre/style tier, an era multiplier, and
    country nudges (Greece + classic international digging territories).
    Always strictly positive so nothing is ever hard-excluded.
    """
    styles = list(styles or [])
    genres = list(genres or [])

    style_w = _best_style_weight(styles)
    genre_w = _best_genre_weight(genres)

    # Prefer the more specific style signal; fall back to genre; then
    # neutral. Blend the two when both exist so a weak genre doesn't drag
    # down a strong style and vice-versa.
    if style_w is not None and genre_w is not None:
        base = max(style_w, (style_w + genre_w) / 2.0)
    elif style_w is not None:
        base = style_w
    elif genre_w is not None:
        base = genre_w
    else:
        base = _TIER_NEUTRAL

    score = base * era_weight(year)

    # Country nudges
    if is_greek(styles, country):
        score *= _GREECE_BONUS
    elif country and _norm(country) in _INTL_DIG_COUNTRIES:
        score *= _INTL_DIG_BONUS

    return score


def blended_score(
    desirability: float,
    affinity: float,
    intensity: float,
) -> float:
    """
    Fold `affinity` into the raw Discogs `desirability` at the given
    `intensity` (0..1).

    intensity=0 → pure desirability (affinity ignored).
    intensity=1 → affinity dominates.

    We add a small floor to desirability so records with a zero want/have
    ratio (no community stats yet) can still ride their sample affinity —
    otherwise brand-new obscure uploads with great affinity would score 0.
    """
    intensity = max(0.0, min(1.0, intensity))
    d = max(desirability, 0.05)
    # Interpolate the affinity exponent: at intensity 0 the affinity term
    # collapses to 1.0 (no effect); at 1.0 it applies fully.
    affinity_term = affinity ** intensity
    return d * affinity_term


# ─── Wide-open dig seeds ─────────────────────────────────────────────
# When the user sets no filters, a bare Discogs master search returns the
# globally most-owned records (have counts in the tens of thousands). Those
# are all rejected by max_have (~3000). Pick a sample-friendly genre or
# style at random so "Dig with no filters" still surfaces diggable gems.

_DISCOGS_DIG_GENRES: tuple[tuple[str, float], ...] = (
    ("Funk / Soul", _TIER_PRIME),
    ("Jazz", _TIER_PRIME),
    ("Latin", _TIER_STRONG),
    ("Stage & Screen", _TIER_STRONG),
    ("Reggae", _TIER_GOOD),
    ("Folk, World, & Country", _TIER_GOOD),
    ("Blues", _TIER_GOOD),
    ("Rock", _TIER_GOOD),
)

_DISCOGS_DIG_STYLES: tuple[tuple[str, float], ...] = (
    ("Soul", _TIER_PRIME),
    ("Funk", _TIER_PRIME),
    ("Breaks", _TIER_PRIME),
    ("Soul-Jazz", _TIER_PRIME),
    ("Jazz-Funk", _TIER_PRIME),
    ("Library Music", _TIER_PRIME),
    ("Soundtrack", _TIER_STRONG),
    ("MPB", _TIER_STRONG),
    ("Boogaloo", _TIER_STRONG),
    ("Afrobeat", _TIER_STRONG),
    ("Ethio-jazz", _TIER_STRONG),
    ("Highlife", _TIER_STRONG),
    ("Dub", _TIER_STRONG),
    ("Gospel", _TIER_STRONG),
    ("Psychedelic Rock", _TIER_STRONG),
    ("Prog Rock", _TIER_GOOD),
    ("Éntekhno", _TIER_STRONG),
    ("Laïkó", _TIER_STRONG),
    ("Rebetiko", _TIER_STRONG),
)


def pick_wide_open_discogs_seed(rng: random.Random) -> tuple[str, str]:
    """
    Return a (field, value) pair for a filterless Dig — either
    ``("genre", "Jazz")`` or ``("style", "Afrobeat")``, weighted toward
    sample-friendly crates.
    """
    if rng.random() < 0.7:
        labels, weights = zip(*_DISCOGS_DIG_GENRES)
        return "genre", rng.choices(labels, weights=weights, k=1)[0]
    labels, weights = zip(*_DISCOGS_DIG_STYLES)
    return "style", rng.choices(labels, weights=weights, k=1)[0]

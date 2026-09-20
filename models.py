"""Data models for the Steam deal card plugin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class GameCandidate:
    """A possible Steam match for a user supplied game name.

    Attributes:
        appid: Steam application id.
        name: Localized display name from the provider.
        steam_name: Name as Steam spells it, used for cross language matching.
        source: Which provider produced the candidate.
        score: Relevance score assigned by the matcher.
        popularity: Follow count used as a tie breaker.
    """

    appid: int
    name: str
    steam_name: str = ""
    source: str = "heybox"
    score: float = 0.0
    popularity: int = 0

    @property
    def display_name(self) -> str:
        """Name to show the user, preferring the localized title."""
        return self.name or self.steam_name


@dataclass(frozen=True)
class ReviewSummary:
    """Aggregated Steam user review figures."""

    label: str
    percent_positive: int
    review_count: int


@dataclass(frozen=True)
class PriceInfo:
    """Current Steam price for one region."""

    formatted_current: str
    formatted_original: str
    discount_percent: int
    currency: str = ""
    current_value: Decimal | None = None
    original_value: Decimal | None = None
    discount_end: datetime | None = None

    @property
    def is_discounted(self) -> bool:
        """Whether the item currently carries a discount."""
        return self.discount_percent > 0


@dataclass(frozen=True)
class LowestPrice:
    """All time lowest price reported by Heybox."""

    value: Decimal
    currency: str
    recorded_on: str
    discount_percent: int


@dataclass(frozen=True)
class GameCard:
    """Everything needed to render one game card."""

    appid: int
    name: str
    price: PriceInfo | None
    reviews: ReviewSummary | None
    capsule_urls: tuple[str, ...] = ()
    lowest: LowestPrice | None = None
    release_date: str = ""
    developers: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    short_description: str = ""
    is_free: bool = False

    @property
    def capsule_url(self) -> str:
        """Preferred capsule image URL, or an empty string when there is none."""
        return self.capsule_urls[0] if self.capsule_urls else ""

    @property
    def store_url(self) -> str:
        """Canonical Steam store URL for this appid."""
        return f"https://store.steampowered.com/app/{self.appid}/"


@dataclass(frozen=True)
class DealItem:
    """One entry in the Steam specials list."""

    appid: int
    name: str
    price: PriceInfo
    capsule_urls: tuple[str, ...] = ()
    lowest: LowestPrice | None = None
    reviews: ReviewSummary | None = None
    release: str = ""

    @property
    def capsule_url(self) -> str:
        """Preferred capsule image URL, or an empty string when there is none."""
        return self.capsule_urls[0] if self.capsule_urls else ""

    @property
    def store_url(self) -> str:
        """Canonical Steam store URL for this appid."""
        return f"https://store.steampowered.com/app/{self.appid}/"


@dataclass(frozen=True)
class PlayerCount:
    """Live concurrent player figures for one app.

    Attributes:
        appid: Steam application id.
        name: Localized display name.
        players: Concurrent players right now, or None when unavailable.
        peak_today: The peak figure Steam's most-played chart reports for this
            game. Steam does not document the exact window and it can read
            lower than ``players``, so it is shown as a separate labelled
            figure and never as the live count.
        rank: Position within the requested list, assigned after sorting.
        capsule_urls: Candidate cover image URLs, best first.
    """

    appid: int
    name: str
    players: int | None = None
    peak_today: int | None = None
    rank: int = 0
    capsule_urls: tuple[str, ...] = ()

    @property
    def capsule_url(self) -> str:
        """Preferred capsule image URL, or an empty string when there is none."""
        return self.capsule_urls[0] if self.capsule_urls else ""

    @property
    def store_url(self) -> str:
        """Canonical Steam store URL for this appid."""
        return f"https://store.steampowered.com/app/{self.appid}/"


def to_decimal(value: object) -> Decimal | None:
    """Convert a loosely typed API value into a Decimal.

    Args:
        value: Raw value taken from an API response.

    Returns:
        The parsed Decimal, or None when the value is missing or not numeric.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

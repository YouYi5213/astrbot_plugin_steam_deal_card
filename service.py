"""Lookup orchestration shared by the command handlers and the tests."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace

import httpx

from .models import DealItem, GameCandidate, GameCard
from .name_match import is_confident, rank_candidates
from .render import render_candidates, render_deals_card, render_game_card
from .steam_api import (
    HeyboxClient,
    SteamApiError,
    SteamStoreClient,
    build_deal_item,
    build_game_card,
)

_APPID_RE = re.compile(r"^\s*(?:appid[=: ]*)?(\d{3,10})\s*$", re.I)
_STEAM_URL_RE = re.compile(r"store\.steampowered\.com/app/(\d+)", re.I)

# How many candidates to keep for the disambiguation list.
_CANDIDATE_LIMIT = 8


class LookupError(RuntimeError):
    """Raised when a lookup cannot produce a usable result."""


@dataclass(frozen=True)
class GameLookupResult:
    """Outcome of resolving a user supplied game name.

    Attributes:
        card: The resolved game, when resolution succeeded.
        candidates: Ranked options when the name was ambiguous.
        query: The original user query.
    """

    card: GameCard | None
    candidates: tuple[GameCandidate, ...]
    query: str

    @property
    def is_ambiguous(self) -> bool:
        """Whether the user must choose between several games."""
        return self.card is None and bool(self.candidates)


def extract_appid(text: str) -> int | None:
    """Pull a Steam appid out of a raw query.

    Args:
        text: User input, which may be a bare appid, ``appid=NNN`` or a store URL.

    Returns:
        The appid, or None when the input does not contain one.
    """
    url_match = _STEAM_URL_RE.search(text or "")
    if url_match:
        return int(url_match.group(1))
    bare_match = _APPID_RE.match(text or "")
    if bare_match:
        return int(bare_match.group(1))
    return None


class SteamDealService:
    """Coordinates Heybox name resolution with Steam storefront data."""

    def __init__(
        self,
        store: SteamStoreClient,
        heybox: HeyboxClient,
        http: httpx.AsyncClient,
        country: str = "CN",
        history_country: str = "cn",
        max_deals: int = 10,
    ) -> None:
        """Store the collaborators and defaults.

        Args:
            store: Steam storefront client.
            heybox: Heybox client.
            http: Shared HTTP client used for image downloads.
            country: Steam storefront country code.
            history_country: Heybox region code for price history.
            max_deals: Maximum number of games on the deals card.
        """
        self.store = store
        self.heybox = heybox
        self.http = http
        self.country = country
        self.history_country = history_country
        self.max_deals = max(max_deals, 1)

    async def resolve_game(self, query: str) -> GameLookupResult:
        """Resolve a query into a single game or a candidate list.

        Args:
            query: Raw user input: a game name, appid or Steam store URL.

        Returns:
            The lookup result, which is either resolved or ambiguous.

        Raises:
            LookupError: If no game could be found at all.
        """
        text = (query or "").strip()
        if not text:
            raise LookupError("请输入游戏名，例如：steam游戏 泰拉瑞亚")

        appid = extract_appid(text)
        if appid is not None:
            card = await self.build_card(appid)
            return GameLookupResult(card=card, candidates=(), query=text)

        candidates = await self.heybox.search(text)
        if not candidates:
            raise LookupError(f"没有找到与「{text}」匹配的 Steam 游戏，请检查名称。")

        # Heybox answers with localized titles, so an English query would not
        # match its own results. Pull Steam's names for the shortlist and score
        # against both spellings.
        candidates = await self._attach_steam_names(candidates)
        ranked = rank_candidates(text, candidates, limit=_CANDIDATE_LIMIT)
        # A best score of zero means no candidate name relates to the query at
        # all; offering those as choices would just be noise.
        if not ranked or ranked[0].score <= 0:
            raise LookupError(f"没有找到与「{text}」匹配的 Steam 游戏，请检查名称。")
        # Keep only candidates that actually relate to the query, so the choice
        # list does not fill up with unrelated games.
        ranked = [candidate for candidate in ranked if candidate.score > 0]

        if is_confident(ranked):
            card = await self.build_card(ranked[0].appid)
            return GameLookupResult(card=card, candidates=(), query=text)

        return GameLookupResult(card=None, candidates=tuple(ranked), query=text)

    async def _attach_steam_names(self, candidates: list[GameCandidate]) -> list[GameCandidate]:
        """Fill in each candidate's Steam storefront name.

        Args:
            candidates: Candidates returned by Heybox.

        Returns:
            The same candidates with ``steam_name`` populated where available.
            Failures leave the candidates untouched.
        """
        appids = [candidate.appid for candidate in candidates]
        if not appids:
            return candidates
        try:
            items = await self.store.get_items(appids, self.country)
        except SteamApiError:
            return candidates
        return [
            replace(candidate, steam_name=str((items.get(candidate.appid) or {}).get("name") or ""))
            for candidate in candidates
        ]

    async def build_card(self, appid: int) -> GameCard:
        """Fetch everything needed for one game card.

        Args:
            appid: Steam application id.

        Returns:
            The assembled game card.

        Raises:
            LookupError: If Steam has no data for the appid.
        """
        items, lowest = await asyncio.gather(
            self.store.get_items([appid], self.country),
            self._safe_lowest(appid),
        )
        item = items.get(appid)
        if item is None:
            raise LookupError(f"Steam 商店没有 appid={appid} 的数据。")
        return build_game_card(appid, item, lowest)

    async def deals(self, limit: int | None = None) -> list[DealItem]:
        """Fetch the current Steam specials with lowest prices attached.

        Args:
            limit: Override for the number of games to return.

        Returns:
            The discounted games, cheapest discount first is not applied; the
            storefront ordering is preserved.

        Raises:
            LookupError: If the specials list cannot be fetched.
        """
        wanted = max(limit or self.max_deals, 1)
        rows = await self.store.specials(self.country, limit=wanted)
        if not rows:
            raise LookupError("暂时没有获取到 Steam 折扣游戏。")

        appids = [row["appid"] for row in rows]
        items, *lowests = await asyncio.gather(
            self.store.get_items(appids, self.country),
            *[self._safe_lowest(appid) for appid in appids],
        )
        lowest_by_appid = dict(zip(appids, lowests, strict=True))

        deals: list[DealItem] = []
        for row in rows:
            deal = build_deal_item(row, items.get(row["appid"]), lowest_by_appid.get(row["appid"]))
            if deal is not None:
                deals.append(deal)
        if not deals:
            raise LookupError("暂时没有获取到 Steam 折扣游戏。")
        return deals

    async def render_game(self, card: GameCard) -> bytes:
        """Render a game card, downloading its capsule image.

        Args:
            card: Game data to render.

        Returns:
            PNG image bytes.
        """
        capsule = await self._download(card.capsule_url)
        return await asyncio.to_thread(render_game_card, card, capsule)

    async def render_deals(self, deals: list[DealItem]) -> bytes:
        """Render the deals card, downloading all capsule images in parallel.

        Args:
            deals: Discounted games to render.

        Returns:
            PNG image bytes.
        """
        images = await asyncio.gather(*[self._download(deal.capsule_url) for deal in deals])
        capsules = {
            deal.appid: data for deal, data in zip(deals, images, strict=True) if data is not None
        }
        return await asyncio.to_thread(render_deals_card, deals, capsules)

    async def render_candidate_list(
        self,
        query: str,
        candidates: tuple[GameCandidate, ...],
        command: str,
    ) -> bytes:
        """Render the disambiguation list.

        Args:
            query: Original user query.
            candidates: Ranked candidates.
            command: Command name to echo in the selection hint.

        Returns:
            PNG image bytes.
        """
        return await asyncio.to_thread(render_candidates, query, list(candidates), command)

    async def _safe_lowest(self, appid: int):
        """Fetch the lowest price without failing the whole lookup.

        Args:
            appid: Steam application id.

        Returns:
            The lowest price, or None when Heybox has no data.
        """
        try:
            return await self.heybox.lowest_price(appid, self.history_country)
        except SteamApiError:
            return None

    async def _download(self, url: str) -> bytes | None:
        """Download an image, tolerating failure.

        Args:
            url: Absolute image URL.

        Returns:
            Image bytes, or None when the download failed.
        """
        if not url:
            return None
        try:
            response = await self.http.get(url)
            response.raise_for_status()
            return response.content
        except Exception:  # noqa: BLE001 - a missing image must not break the card
            return None

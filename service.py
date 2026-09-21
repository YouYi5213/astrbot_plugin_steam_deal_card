"""Lookup orchestration shared by the command handlers and the tests."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from astrbot.api import logger

from .models import DealItem, GameCandidate, GameCard, PlayerCount, PriceInfo, ReviewSummary
from .name_match import is_confident, rank_candidates
from .render import (
    render_candidates,
    render_deals_card,
    render_free_card,
    render_game_card,
    render_players_card,
    render_ranking_card,
)
from .steam_api import (
    HeyboxClient,
    SteamApiError,
    SteamSearchClient,
    SteamStoreClient,
    _format_rollup_date,
    build_deal_item,
    build_game_card,
    capsule_urls,
)

_APPID_RE = re.compile(r"^\s*(?:appid[=: ]*)?(\d{3,10})\s*$", re.I)
_STEAM_URL_RE = re.compile(r"store\.steampowered\.com/app/(\d+)", re.I)

# Cache timestamps are rendered for a CN audience.
_BEIJING = timezone(timedelta(hours=8))

# How many candidates to keep for the disambiguation list.
_CANDIDATE_LIMIT = 8

# The chart is ranked by a completed day's peak, so the live order differs;
# pull a wider candidate pool than the user asked for and re-rank by live counts.
_CHART_POOL = 100
# Player counts are one request per app, so cap how many run at once to stay a
# good citizen without making the user wait for a serial loop.
_PLAYER_CONCURRENCY = 12

# Placeholder price shown for games that cost nothing. Shared with the builder
# so the "is this free" test stays in one place.
_FREE_LABEL = "免费游玩"


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


def _dedupe_candidates(candidates: list[GameCandidate]) -> list[GameCandidate]:
    """Collapse candidates that share an appid, keeping the richest entry.

    Both sources can return the same game, and their names differ by language,
    so the entry carrying the most information is kept.

    Args:
        candidates: Candidates from every source, in source order.

    Returns:
        One candidate per appid, in first-seen order.
    """
    merged: dict[int, GameCandidate] = {}
    for candidate in candidates:
        existing = merged.get(candidate.appid)
        if existing is None:
            merged[candidate.appid] = candidate
            continue
        # Prefer a name the matcher can use against the query, then popularity.
        if (candidate.popularity, len(candidate.name)) > (
            existing.popularity,
            len(existing.name),
        ):
            merged[candidate.appid] = candidate
    return list(merged.values())


def _parse_cached_time(value: object) -> datetime | None:
    """Convert a cached epoch second back into an aware UTC datetime.

    Args:
        value: Stored timestamp, or None when the entry had no deadline.

    Returns:
        The datetime in UTC, or None when the value is missing or unusable.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


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
        max_players: int = 20,
        search: SteamSearchClient | None = None,
        free_cache_path: Path | None = None,
    ) -> None:
        """Store the collaborators and defaults.

        Args:
            store: Steam storefront client.
            heybox: Heybox client.
            http: Shared HTTP client used for image downloads.
            country: Steam storefront country code.
            history_country: Heybox region code for price history.
            max_deals: Maximum number of games on the deals card.
            max_players: Maximum number of games on the player count card.
            search: Fallback name resolver, defaulting to a storefront search
                built from the shared HTTP client.
            free_cache_path: Where to keep the last giveaway list. Only the
                global storefront carries giveaways, and it is intermittently
                unreachable from a mainland server, so the last good answer is
                worth keeping. None disables caching.
        """
        self.store = store
        self.heybox = heybox
        self.http = http
        self.country = country
        self.history_country = history_country
        self.max_deals = max(max_deals, 1)
        self.max_players = max(max_players, 1)
        self.search = search or SteamSearchClient(http)
        self.free_cache_path = free_cache_path
        # Set when the giveaway list was served from the cache, so the command
        # can say so instead of passing stale data off as current.
        self.free_cache_note = ""

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

        candidates, degraded = await self._search_candidates(text)
        if not candidates:
            if degraded:
                raise LookupError(
                    f"暂时无法解析「{text}」：中文名转换服务（小黑盒）当前不可用。\n"
                    "可以先用英文名或 appid 查询，例如：steam游戏 105600"
                )
            raise LookupError(f"没有找到与「{text}」匹配的 Steam 游戏，请检查名称。")

        # Heybox answers with localized titles, so an English query would not
        # match its own results. Pull Steam's names for the shortlist and score
        # against both spellings.
        candidates = await self._attach_steam_names(candidates)
        ranked = rank_candidates(text, candidates, limit=_CANDIDATE_LIMIT)
        # A best score of zero means no candidate name relates to the query at
        # all; offering those as choices would just be noise.
        if not ranked or ranked[0].score <= 0:
            if degraded:
                raise LookupError(
                    f"暂时无法解析「{text}」：中文名转换服务（小黑盒）当前不可用。\n"
                    "可以先用英文名或 appid 查询，例如：steam游戏 105600"
                )
            raise LookupError(f"没有找到与「{text}」匹配的 Steam 游戏，请检查名称。")
        # Keep only candidates that actually relate to the query, so the choice
        # list does not fill up with unrelated games.
        ranked = [candidate for candidate in ranked if candidate.score > 0]

        if is_confident(ranked):
            card = await self.build_card(ranked[0].appid)
            return GameLookupResult(card=card, candidates=(), query=text)

        return GameLookupResult(card=None, candidates=tuple(ranked), query=text)

    async def _search_candidates(self, text: str) -> tuple[list[GameCandidate], bool]:
        """Resolve a name to candidates by consulting both sources.

        Heybox understands colloquial Chinese names, but it gives its own
        synthetic ids to games it has no Steam id for and those are dropped, so
        it cannot resolve everything. Steam's own search understands official
        localized titles, which covers exactly that gap. Neither is a superset
        of the other, so the results are merged and ranked together.

        Args:
            text: Raw user supplied game name.

        Returns:
            A tuple of the candidates and whether Heybox was unavailable.
        """
        candidates: list[GameCandidate] = []
        degraded = False
        try:
            candidates = await self.heybox.search(text)
        except SteamApiError as exc:
            logger.warning(f"Heybox search failed, falling back to Steam search: {exc}")
            degraded = True

        try:
            candidates += await self.search.search(text)
        except SteamApiError as exc:
            logger.warning(f"Steam search also failed: {exc}")

        return _dedupe_candidates(candidates), degraded

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

    async def popular_upcoming(self, limit: int | None = None) -> list[DealItem]:
        """Fetch Steam's 热门即将推出 shelf.

        Args:
            limit: Override for the number of games to return.

        Returns:
            The games, in the shelf's own order.

        Raises:
            LookupError: If the shelf cannot be fetched.
        """
        wanted = max(limit or self.max_deals, 1)
        rows = await self.store.popular_upcoming(self.country, limit=wanted)
        if not rows:
            raise LookupError("暂时没有获取到 Steam 即将推出的游戏。")
        return await self._enrich_rows(rows, "暂时没有获取到 Steam 即将推出的游戏。")

    async def free_games(self, limit: int | None = None) -> list[DealItem]:
        """Fetch the games currently free to keep for a limited time.

        Unlike the ordinary deals list, the deadline is the whole point here:
        once the promotion ends the game is gone from the account forever, so
        the end date is kept rather than stripped.

        Only the global storefront carries giveaways, and that host is
        intermittently unreachable from a mainland server, so the last good list
        is cached and reused rather than reporting an error. The reused list is
        flagged through ``free_cache_note`` so it is never passed off as current.

        Args:
            limit: Override for the number of games to return.

        Returns:
            The giveaways, in the storefront's own order.

        Raises:
            LookupError: If the listing cannot be fetched and no usable cache
                entry exists.
        """
        wanted = max(limit or self.max_deals, 1)
        self.free_cache_note = ""
        try:
            rows = await self.store.specials(self.country, limit=wanted, free_only=True)
        except SteamApiError:
            cached = self._load_free_cache(wanted)
            if cached is None:
                raise
            deals, self.free_cache_note = cached
            return deals
        if not rows:
            raise LookupError("暂时没有可以免费领取的游戏。")
        deals = await self._enrich_rows(
            rows,
            "暂时没有可以免费领取的游戏。",
            keep_free_deadline=True,
        )
        self._save_free_cache(deals)
        return deals

    def _load_free_cache(self, limit: int) -> tuple[list[DealItem], str] | None:
        """Rebuild the cached giveaway list, dropping anything already expired.

        Args:
            limit: How many entries the caller wanted.

        Returns:
            The rebuilt entries and the note explaining their age, or None when
            there is no cache or nothing in it is still claimable.
        """
        if self.free_cache_path is None:
            return None
        try:
            payload = json.loads(self.free_cache_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromtimestamp(int(payload["fetched_at"]), tz=timezone.utc)
            raw_items = payload["items"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(raw_items, list):
            return None

        now = datetime.now(timezone.utc)
        deals: list[DealItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            end = _parse_cached_time(raw.get("end"))
            # A deadline that has passed is worse than no entry at all: the
            # user cannot claim it any more.
            if end is not None and end <= now:
                continue
            try:
                appid = int(raw["appid"])
                name = str(raw["name"])
            except (KeyError, TypeError, ValueError):
                continue
            caps = raw.get("capsules") or []
            review_count = int(raw.get("review_count") or 0)
            deals.append(
                DealItem(
                    appid=appid,
                    name=name,
                    price=PriceInfo(
                        formatted_current=str(raw.get("current") or ""),
                        formatted_original=str(raw.get("original") or ""),
                        discount_percent=int(raw.get("percent") or 0),
                        discount_end=end,
                        is_giveaway=True,
                    ),
                    capsule_urls=tuple(str(c) for c in caps if c),
                    reviews=(
                        ReviewSummary(
                            label=str(raw.get("review_label") or ""),
                            percent_positive=int(raw.get("review_percent") or 0),
                            review_count=review_count,
                        )
                        if review_count
                        else None
                    ),
                )
            )
        if not deals:
            return None
        stamp = fetched_at.astimezone(_BEIJING).strftime("%m-%d %H:%M")
        note = f"商店暂时不可达，以下为 {stamp} 缓存的结果，可能不是最新"
        return deals[:limit], note

    def _save_free_cache(self, deals: list[DealItem]) -> None:
        """Write the giveaway list to disk, ignoring any write failure.

        Args:
            deals: The freshly fetched giveaways.
        """
        if self.free_cache_path is None:
            return
        items = [
            {
                "appid": deal.appid,
                "name": deal.name,
                "capsules": list(deal.capsule_urls),
                "current": deal.price.formatted_current,
                "original": deal.price.formatted_original,
                "percent": deal.price.discount_percent,
                "end": (
                    int(deal.price.discount_end.timestamp())
                    if deal.price.discount_end is not None
                    else None
                ),
                "review_label": deal.reviews.label if deal.reviews else "",
                "review_percent": deal.reviews.percent_positive if deal.reviews else 0,
                "review_count": deal.reviews.review_count if deal.reviews else 0,
            }
            for deal in deals
        ]
        payload = {"fetched_at": int(datetime.now(timezone.utc).timestamp()), "items": items}
        try:
            self.free_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.free_cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            # A cache that cannot be written is not worth failing the lookup for.
            logger.warning("写入喜加一缓存失败：%s", self.free_cache_path)

    async def _enrich_rows(
        self,
        rows: list[dict],
        empty_message: str,
        keep_free_deadline: bool = False,
    ) -> list[DealItem]:
        """Attach store details and lowest prices to listing rows.

        Args:
            rows: Raw listing rows.
            empty_message: Error text when every row fails to build.
            keep_free_deadline: Keep the discount end date on free entries. It is
                dropped for ordinary free games, where the date means nothing,
                but for a giveaway it is the deadline to claim by.

        Returns:
            The built items, rows that cannot be built being skipped.

        Raises:
            LookupError: If no row could be built.
        """
        appids = [row["appid"] for row in rows]
        items, *lowests = await asyncio.gather(
            self.store.get_items(appids, self.country),
            *[self._safe_lowest(appid) for appid in appids],
        )
        lowest_by_appid = dict(zip(appids, lowests, strict=True))

        built: list[DealItem] = []
        for row in rows:
            appid = row["appid"]
            item = build_deal_item(
                row,
                items.get(appid),
                lowest_by_appid.get(appid),
                allow_free=True,
            )
            if item is None:
                continue
            # Neither a free-to-play game nor a giveaway has a meaningful
            # historical low, and Heybox would otherwise report a figure for a
            # paid edition of the same title. A giveaway is recognised by its
            # own flag: it reads as "¥0.00" rather than the free-to-play label.
            if item.price.is_giveaway or item.price.formatted_current == _FREE_LABEL:
                item = replace(item, lowest=None)
                if not keep_free_deadline:
                    item = replace(item, price=replace(item.price, discount_end=None))
            built.append(item)
        if not built:
            raise LookupError(empty_message)
        return built

    async def player_count(self, appid: int) -> PlayerCount:
        """Fetch the live player count for one game.

        Args:
            appid: Steam application id.

        Returns:
            The player count, with the game's name when Steam reports one.

        Raises:
            LookupError: If Steam has no data for the appid.
        """
        counts, items = await asyncio.gather(
            self._player_counts([appid]),
            self._safe_items([appid]),
        )
        players = counts.get(appid)
        item = items.get(appid)
        if players is None and item is None:
            raise LookupError(f"Steam 没有 appid={appid} 的数据。")
        name = _item_name(item) or f"appid {appid}"
        peak, peak_date = await self._safe_peak(appid)
        return PlayerCount(
            appid=appid,
            name=name,
            players=players,
            peak=peak,
            peak_date=peak_date,
            rank=1,
            capsule_urls=capsule_urls(item) if item else (),
        )

    async def top_players(self, limit: int | None = None) -> list[PlayerCount]:
        """Rank games by the number of players in them right now.

        Steam's most-played chart is ordered by a completed day's peak, which is
        a poor proxy for the live order, so the chart is used only to pick
        candidates and the returned list is sorted by the live counts. The peak
        still travels with each row, carrying the day it belongs to.

        Args:
            limit: How many games to return.

        Returns:
            The games, most players first.

        Raises:
            LookupError: If the chart or every player count could not be read.
        """
        wanted = max(limit or self.max_players, 1)
        rows = await self.store.most_played(_CHART_POOL)
        if not rows:
            raise LookupError("暂时没有获取到 Steam 在线人数榜。")

        # The chart is already roughly ordered by popularity, so asking about
        # its first rows is the cheapest way to find the true top N.
        pool = rows[: min(len(rows), max(wanted * 3, wanted + 10))]
        appids = [row["appid"] for row in pool]
        counts, items = await asyncio.gather(
            self._player_counts(appids),
            self._safe_items(appids),
        )

        peaks = {row["appid"]: row["peak_in_game"] for row in rows}
        # Every row shares one rollup date; it names the day the peaks cover.
        peak_date = _format_rollup_date(rows[0].get("rollup_date"))
        ranked = [
            PlayerCount(
                appid=appid,
                name=_item_name(items.get(appid)) or f"appid {appid}",
                players=players,
                peak=peaks.get(appid),
                peak_date=peak_date,
                capsule_urls=capsule_urls(items[appid]) if appid in items else (),
            )
            for appid, players in counts.items()
            if players is not None
        ]
        if not ranked:
            raise LookupError("暂时没有获取到 Steam 在线人数。")

        ranked.sort(key=lambda entry: entry.players or 0, reverse=True)
        return [replace(entry, rank=index) for index, entry in enumerate(ranked[:wanted], start=1)]

    async def _safe_peak(self, appid: int) -> tuple[int | None, str]:
        """Look up a game's charted peak without failing the lookup.

        A single game is not necessarily on the chart, and the chart call is
        only for extra context, so a failure must not break the lookup.

        Args:
            appid: Steam application id.

        Returns:
            The peak figure and the ``MM-DD`` day it covers; both empty when the
            chart omits the game or cannot be read.
        """
        try:
            rows = await self.store.most_played(_CHART_POOL)
        except Exception:  # noqa: BLE001 - context only, never fatal
            return None, ""
        for row in rows:
            if row["appid"] == appid:
                peak = row["peak_in_game"] or None
                return peak, _format_rollup_date(row.get("rollup_date")) if peak else ""
        return None, ""

    async def _player_counts(self, appids: list[int]) -> dict[int, int | None]:
        """Fetch live player counts for several apps at once.

        Args:
            appids: Steam application ids.

        Returns:
            A mapping of appid to player count. Apps Steam does not report are
            present with a None value; apps whose request failed are omitted.
        """
        if not appids:
            return {}
        gate = asyncio.Semaphore(_PLAYER_CONCURRENCY)

        async def one(appid: int) -> tuple[int, int | None] | None:
            async with gate:
                try:
                    return appid, await self.store.current_players(appid)
                except Exception:  # noqa: BLE001 - one bad app must not fail the list
                    return None

        results = await asyncio.gather(*[one(appid) for appid in appids])
        return {appid: count for result in results if result for appid, count in (result,)}

    async def _safe_items(self, appids: list[int]) -> dict[int, dict]:
        """Fetch store items, treating a failure as missing data.

        Args:
            appids: Steam application ids.

        Returns:
            A mapping of appid to raw store item, empty when the call failed.
        """
        try:
            return await self.store.get_items(appids, self.country)
        except SteamApiError:
            return {}

    async def render_players(
        self,
        entries: list[PlayerCount],
        title: str | None = None,
    ) -> bytes:
        """Render the player count card, downloading cover images in parallel.

        Args:
            entries: Ranked player counts.
            title: Heading override; defaults to a ranking or info heading
                depending on how many entries were supplied.

        Returns:
            PNG image bytes.
        """
        images = await asyncio.gather(
            *[self._download_first(entry.capsule_urls) for entry in entries]
        )
        capsules = {
            entry.appid: data
            for entry, data in zip(entries, images, strict=True)
            if data is not None
        }
        if title is None:
            title = "Steam 实时在线" if len(entries) == 1 else "Steam 在线人数排行"
        return await asyncio.to_thread(render_players_card, entries, capsules, title)

    async def render_game(self, card: GameCard) -> bytes:
        """Render a game card, downloading its capsule image.

        Args:
            card: Game data to render.

        Returns:
            PNG image bytes.
        """
        capsule = await self._download_first(card.capsule_urls)
        return await asyncio.to_thread(render_game_card, card, capsule)

    async def render_deals(self, deals: list[DealItem]) -> bytes:
        """Render the deals card, downloading all capsule images in parallel.

        Args:
            deals: Discounted games to render.

        Returns:
            PNG image bytes.
        """
        images = await asyncio.gather(*[self._download_first(deal.capsule_urls) for deal in deals])
        capsules = {
            deal.appid: data for deal, data in zip(deals, images, strict=True) if data is not None
        }
        return await asyncio.to_thread(render_deals_card, deals, capsules)

    async def render_free(self, deals: list[DealItem]) -> bytes:
        """Render the giveaway card, downloading all capsule images in parallel.

        Args:
            deals: Giveaways to render.

        Returns:
            PNG image bytes.
        """
        images = await asyncio.gather(*[self._download_first(deal.capsule_urls) for deal in deals])
        capsules = {
            deal.appid: data for deal, data in zip(deals, images, strict=True) if data is not None
        }
        return await asyncio.to_thread(render_free_card, deals, capsules, note=self.free_cache_note)

    async def render_ranking(
        self,
        items: list[DealItem],
        *,
        title: str,
        subtitle: str = "",
        note: str = "",
        show_lowest: bool = False,
        show_release: bool = False,
    ) -> bytes:
        """Render a ranking list, downloading all capsule images in parallel.

        Args:
            items: Games to render, in display order.
            title: Heading for the card.
            subtitle: Small line beside the heading.
            note: Optional explanation under the heading.
            show_lowest: Draw the all time lowest price line.
            show_release: Draw each game's release date line.

        Returns:
            PNG image bytes.
        """
        images = await asyncio.gather(*[self._download_first(item.capsule_urls) for item in items])
        capsules = {
            item.appid: data for item, data in zip(items, images, strict=True) if data is not None
        }
        return await asyncio.to_thread(
            render_ranking_card,
            items,
            capsules,
            title,
            subtitle,
            None,
            show_lowest,
            False,
            False,
            show_release,
            note,
        )

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

    async def _download_first(self, urls: tuple[str, ...]) -> bytes | None:
        """Download the first of several candidate images that resolves.

        Steam capsule art is not always at the canonical path, so the candidates
        are tried in order and the first success wins.

        Args:
            urls: Absolute image URLs in preference order.

        Returns:
            Image bytes, or None when every candidate failed.
        """
        for url in urls:
            data = await self._download(url)
            if data:
                return data
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


def _item_name(item: dict | None) -> str:
    """Read a display name out of a raw store item.

    Args:
        item: Raw store item, or None when Steam had no data.

    Returns:
        The name, or an empty string when it is unavailable.
    """
    if not isinstance(item, dict):
        return ""
    name = item.get("name")
    return name.strip() if isinstance(name, str) else ""

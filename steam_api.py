"""HTTP clients for the public Steam and Heybox endpoints used by the plugin.

No API key is required. The Steam endpoints are public storefront services and
the Heybox endpoints are the ones the Heybox website itself calls.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import (
    DealItem,
    GameCandidate,
    GameCard,
    LowestPrice,
    PriceInfo,
    ReviewSummary,
    to_decimal,
)

STEAM_GET_ITEMS_PATH = "/IStoreBrowseService/GetItems/v1/"
STEAM_PLAYER_COUNT_PATH = "/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_MOST_PLAYED_PATH = "/ISteamChartsService/GetMostPlayedGames/v1/"
# Steam answers the same API on two hosts. The global one is unreachable from
# mainland China hosts (connect hangs until timeout) while the China one is
# fast there, and vice versa is never a problem, so requests fail over between
# them and the first host that answers is remembered for later calls.
STEAM_API_BASES = (
    "https://api.steampowered.com",
    "https://api.steamchina.com",
)
STEAM_GET_ITEMS_URL = STEAM_API_BASES[0] + STEAM_GET_ITEMS_PATH
# The storefront search answers on both the global and the China host with the
# same JSON envelope and the same markup. The global host is frequently
# unreachable from mainland China servers, so both are tried in turn; see
# _search_json. The China host carries a much smaller catalogue (13 rows for
# popularnew against 369 globally), so it is a fallback, never the default.
STEAM_SEARCH_BASES = (
    "https://store.steampowered.com",
    "https://store.steamchina.com",
)
STEAM_SEARCH_PATH = "/search/results/"
STEAM_SEARCH_RESULTS_URL = STEAM_SEARCH_BASES[0] + STEAM_SEARCH_PATH
# The 热门新品 ranking lives only on this server-rendered page. No search
# parameter combination reproduces it; see popular_new.
STEAM_EXPLORE_NEW_URL = "https://store.steampowered.com/explore/new/"
STEAM_FEATURED_CATEGORIES_URL = "https://store.steampowered.com/api/featuredcategories/"
# JSON storefront search, used only as a Heybox outage fallback. The older
# /search/suggest endpoint returns HTML and no longer answers JSON, so this is
# the reliable structured option. It does not understand Chinese names.
STEAM_SEARCH_SUGGEST_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
# Capsule images live at predictable paths, which matters because the China API
# does not report a main_capsule filename the way the global one does.
STEAM_CAPSULE_FALLBACKS = ("capsule_616x353.jpg", "header.jpg")
HEYBOX_SEARCH_URL = "https://api.xiaoheihe.cn/game/search/"
HEYBOX_HISTORY_URL = "https://api.xiaoheihe.cn/game/get_game_prices/history/v2"
HEYBOX_WEB_BASE = "https://www.xiaoheihe.cn"

# Steam caps the infinite-scroll endpoint at 100 rows per request.
_MAX_PAGE_SIZE = 100
# Keep a single GetItems call within a size the endpoint answers quickly.
_GET_ITEMS_CHUNK = 50
# Budget for the first request to a not-yet-confirmed API host. The endpoint
# normally answers in well under a second, so this only bounds a dead host.
_HOST_PROBE_TIMEOUT = 8.0
# The specials listing host refuses connections intermittently, so each attempt
# gets a short budget and the request is retried a few times.
_SPECIALS_ATTEMPTS = 5
_SPECIALS_ATTEMPT_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

_ROW_RE = re.compile(r'<a[^>]*class="[^"]*search_result_row[^"]*"[\s\S]*?</a>')
_APPID_RE = re.compile(r'data-ds-appid="([\d,]+)"')
_TITLE_RE = re.compile(r'<span class="title">([^<]+)</span>')
_CAPSULE_RE = re.compile(r'<div class="search_capsule">\s*<img src="([^"]+)"')
_DISCOUNT_RE = re.compile(r'data-discount="(\d+)"')
_FINAL_RE = re.compile(r'discount_final_price">([^<]*)<')
_ORIGINAL_RE = re.compile(r'discount_original_price">([^<]*)<')
# The explore page nests its rows in tab_item anchors whose attributes carry
# the appid, so both the opening tag and the body are needed.
_TAB_ITEM_RE = re.compile(r'<a\b[^>]*class="tab_item[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
_TAB_NAME_RE = re.compile(r'tab_item_name">([^<]+)<')
_TAB_CAP_RE = re.compile(r'tab_item_cap_img"[^>]*src="([^"]+)"')
_TAB_DISCOUNT_RE = re.compile(r'data-discount="(\d+)"')


class SteamApiError(RuntimeError):
    """Raised when a required upstream response cannot be used."""


class SteamStoreClient:
    """Client for the public Steam storefront endpoints."""

    def __init__(self, client: httpx.AsyncClient, language: str = "schinese") -> None:
        """Store the shared HTTP client.

        Args:
            client: Shared async HTTP client.
            language: Steam storefront language code.
        """
        self.client = client
        self.language = language
        # Remembered after the first successful call so later requests do not
        # pay the connection timeout of the unreachable host on every lookup.
        self._api_base: str | None = None
        # Set when a listing had to be served from a weaker source, so callers
        # can surface that to the user.
        self.last_fallback_reason: str | None = None

    async def get_items(
        self,
        appids: list[int],
        country: str = "CN",
    ) -> dict[int, dict[str, Any]]:
        """Fetch price, review, asset and metadata for many appids at once.

        Args:
            appids: Steam application ids to look up.
            country: Steam storefront country code.

        Returns:
            Mapping of appid to the raw store item payload. Unknown appids are
            omitted rather than raising.

        Raises:
            SteamApiError: If every known API host fails.
        """
        result: dict[int, dict[str, Any]] = {}
        unique = [appid for appid in dict.fromkeys(appids) if appid > 0]
        for start in range(0, len(unique), _GET_ITEMS_CHUNK):
            chunk = unique[start : start + _GET_ITEMS_CHUNK]
            body = await self._request_items(chunk, country)
            items = (body.get("response") or {}).get("store_items") or []
            for item in items:
                appid = item.get("appid")
                if isinstance(appid, int) and item.get("success", True):
                    result[appid] = item
        return result

    async def _request_items(self, appids: list[int], country: str) -> dict[str, Any]:
        """Request one chunk, failing over between the Steam API hosts.

        Args:
            appids: Steam application ids for this chunk.
            country: Steam storefront country code.

        Returns:
            The decoded response body.

        Raises:
            SteamApiError: If no host answered.
        """
        payload = {
            "ids": [{"appid": appid} for appid in appids],
            "context": {
                "language": self.language,
                "country_code": country,
                "steam_realm": 1,
            },
            "data_request": {
                "include_all_purchase_options": True,
                "include_assets": True,
                "include_reviews": True,
                "include_basic_info": True,
                "include_release": True,
                "include_platforms": True,
            },
        }
        params = {"input_json": json.dumps(payload, separators=(",", ":"))}

        preferred = [self._api_base] if self._api_base else []
        bases = preferred + [base for base in STEAM_API_BASES if base not in preferred]
        failures: list[str] = []
        for base in bases:
            # A blocked host can accept the TCP connection and then hang before
            # sending anything, so the connect timeout alone does not bound the
            # wait. Probing an unconfirmed host uses a tighter budget; once a
            # host is known good the caller's normal timeout applies.
            kwargs = {} if self._api_base else {"timeout": _HOST_PROBE_TIMEOUT}
            try:
                response = await self.client.get(
                    base + STEAM_GET_ITEMS_PATH, params=params, **kwargs
                )
                response.raise_for_status()
                body = response.json()
            except Exception as exc:  # noqa: BLE001 - try the next host
                failures.append(f"{base}（{_describe_error(exc)}）")
                # A cached host that stopped answering must not stay pinned.
                if self._api_base == base:
                    self._api_base = None
                continue
            self._api_base = base
            return body

        raise SteamApiError("Steam 商店数据请求失败：" + "；".join(failures))

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Call a Steam Web API path, failing over between the known hosts.

        Args:
            path: API path beginning with a slash, e.g.
                ``/ISteamUserStats/...``.
            params: Query parameters for the request.

        Returns:
            The decoded JSON body.

        Raises:
            SteamApiError: If no host answered.
        """
        preferred = [self._api_base] if self._api_base else []
        bases = preferred + [base for base in STEAM_API_BASES if base not in preferred]
        failures: list[str] = []
        for base in bases:
            kwargs = {} if self._api_base else {"timeout": _HOST_PROBE_TIMEOUT}
            try:
                response = await self.client.get(base + path, params=params, **kwargs)
                response.raise_for_status()
                body = response.json()
            except Exception as exc:  # noqa: BLE001 - try the next host
                failures.append(f"{base}（{_describe_error(exc)}）")
                if self._api_base == base:
                    self._api_base = None
                continue
            self._api_base = base
            return body

        raise SteamApiError("Steam 数据请求失败：" + "；".join(failures))

    async def current_players(self, appid: int) -> int | None:
        """Fetch the number of players in a game right now.

        Args:
            appid: Steam application id.

        Returns:
            The concurrent player count, or None when Steam does not report one
            (which it does for delisted and some unreleased apps).
        """
        body = await self._api_get(STEAM_PLAYER_COUNT_PATH, {"appid": appid})
        count = (body or {}).get("response", {}).get("player_count")
        return count if isinstance(count, int) and count >= 0 else None

    async def most_played(self, limit: int = 100) -> list[dict[str, int]]:
        """Fetch Steam's most played chart.

        The chart ranks by the day's peak, not by the live count, so treat the
        result as a candidate pool to price against ``current_players`` rather
        than as an ordering to display.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            Row dicts with ``appid`` and ``peak_in_game``, in chart order.
        """
        body = await self._api_get(STEAM_MOST_PLAYED_PATH)
        ranks = (body or {}).get("response", {}).get("ranks") or []
        rows: list[dict[str, int]] = []
        for row in ranks:
            # Guard the shape: an unexpected row must not take down the list.
            if not isinstance(row, dict):
                continue
            appid = row.get("appid")
            if not isinstance(appid, int) or isinstance(appid, bool):
                continue
            peak = row.get("peak_in_game")
            rows.append(
                {
                    "appid": appid,
                    "peak_in_game": peak if isinstance(peak, int) else 0,
                }
            )
        return rows[: max(limit, 1)]

    async def specials(self, country: str = "CN", limit: int = 20) -> list[dict[str, Any]]:
        """Fetch the current Steam specials list.

        Args:
            country: Steam storefront country code.
            limit: Maximum number of discounted entries to return.

        Returns:
            Raw row dicts with appid, name, capsule and price text.

        Raises:
            SteamApiError: If the listing endpoint fails on every attempt.
        """
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        wanted = max(limit, 1)
        start = 0
        while len(rows) < wanted and start < 500:
            count = min(_MAX_PAGE_SIZE, max(wanted * 2, 50))
            body = await self._request_specials_page(country, start, count)
            page = _parse_specials_page(body.get("results_html") or "")
            if not page:
                break
            for row in page:
                if row["appid"] in seen:
                    continue
                seen.add(row["appid"])
                rows.append(row)
            start += count
        return rows[:wanted]

    async def _request_specials_page(
        self,
        country: str,
        start: int,
        count: int,
    ) -> dict[str, Any]:
        """Request one page of the specials list, retrying until it answers.

        This host refuses connections intermittently rather than being blocked
        outright, so a short per-attempt timeout plus a few retries turns a
        frequent failure into a reliably fast success.

        Args:
            country: Steam storefront country code.
            start: Pagination offset.
            count: Rows to request.

        Returns:
            The decoded response body.

        Raises:
            SteamApiError: If every attempt failed.
        """
        params = {
            "query": "",
            "start": start,
            "count": count,
            "specials": 1,
            "infinite": 1,
            "cc": country,
            "l": self.language,
        }
        return await self._search_json(params, "Steam 特惠列表")

    async def popular_new(self, country: str = "CN", limit: int = 10) -> list[dict[str, Any]]:
        """Fetch Steam's 热门新品 (popular new releases) ranking.

        This is the list the store shows on ``/explore/new/``. It is *not*
        reachable through the search endpoint: every ``filter``/``sort_by``
        combination was measured against the store page and overlapped it on
        0 of 10 rows, so the page HTML is the only source. In particular the
        ``popularnew`` search filter is wrong for this, as it ranks by overall
        popularity and returns long-lived games such as CS2 and GTA V.

        Args:
            country: Steam storefront country code.
            limit: Maximum number of entries to return.

        Returns:
            Row dicts with appid, name, capsule and price text.

        Raises:
            SteamApiError: If the page cannot be fetched and the fallback fails.
        """
        wanted = max(limit, 1)
        try:
            response = await self.client.get(
                STEAM_EXPLORE_NEW_URL,
                params={"l": self.language, "cc": country},
            )
            response.raise_for_status()
            # The page ships both tabs as one HTML document, so parse the lot
            # and keep the order the store itself uses.
            rows = parse_explore_tab_items(response.text)
        except Exception as exc:  # noqa: BLE001 - fallback below
            rows = []
            reason = _describe_error(exc)
        else:
            reason = ""

        if rows:
            return rows[:wanted]

        # The explore page only exists on the global host, which is unreachable
        # from some regions. Fall back to the search filter so the command still
        # answers, and record it so the caller can say so.
        body = await self._search_json(
            {
                "query": "",
                "start": 0,
                "count": min(wanted * 2, _MAX_PAGE_SIZE),
                "filter": "popularnew",
                "infinite": 1,
                "cc": country,
                "l": self.language,
            },
            "Steam 热门新品",
        )
        self.last_fallback_reason = reason or "店铺页面未返回条目"
        return _parse_specials_page(body.get("results_html") or "")[:wanted]

    async def popular_upcoming(self, country: str = "CN") -> list[dict[str, Any]]:
        """Fetch Steam's curated upcoming releases.

        Steam offers no popularity ordering for unreleased games: the
        ``comingsoon`` search filter is ordered by release date and its first
        hundred rows carry no reviews at all. The storefront shelf is therefore
        used instead, which is curated by Steam and reliably contains notable
        titles.

        Args:
            country: Steam storefront country code.

        Returns:
            Row dicts with appid, name and capsule URL.

        Raises:
            SteamApiError: If the shelf endpoint fails.
        """
        try:
            response = await self.client.get(
                STEAM_FEATURED_CATEGORIES_URL,
                params={"cc": country, "l": self.language},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - fallback below
            # Only the global host serves the shelf API and it is unreachable
            # from some regions, so fall back to the equivalent search filter.
            # It is the weaker source (release order, many demos) but it beats
            # returning nothing.
            return await self._upcoming_from_search(country, _describe_error(exc))

        section = body.get("coming_soon") if isinstance(body, dict) else None
        items = section.get("items") if isinstance(section, dict) else None
        rows: list[dict[str, Any]] = []
        for entry in items if isinstance(items, list) else []:
            if not isinstance(entry, dict):
                continue
            appid = entry.get("id")
            name = str(entry.get("name") or "").strip()
            if not isinstance(appid, int) or isinstance(appid, bool) or not name:
                continue
            rows.append(
                {
                    "appid": appid,
                    "name": name,
                    "capsule": str(entry.get("header_image") or "").strip(),
                    "original": "",
                    "final": "",
                    "discount": 0,
                }
            )
        return rows

    async def _upcoming_from_search(self, country: str, reason: str) -> list[dict[str, Any]]:
        """Read upcoming games from the search endpoint when the shelf fails.

        Args:
            country: Steam storefront country code.
            reason: Error text from the shelf request, kept for the log.

        Returns:
            Row dicts shaped like the shelf rows.

        Raises:
            SteamApiError: If the search endpoint also fails.
        """
        body = await self._search_json(
            {
                "query": "",
                "start": 0,
                "count": _MAX_PAGE_SIZE,
                "filter": "comingsoon",
                "category1": 998,  # excludes demos, which dominate the raw list
                "infinite": 1,
                "cc": country,
                "l": self.language,
            },
            "Steam 即将推出列表",
        )
        # Recorded rather than logged, so this module stays free of the plugin
        # framework and can be imported on its own.
        self.last_fallback_reason = reason
        return _parse_specials_page(body.get("results_html") or "")

    async def _search_json(self, params: dict[str, Any], what: str) -> dict[str, Any]:
        """Request a storefront search page, retrying hosts until one answers.

        Each host refuses connections intermittently rather than being blocked
        outright, so a short per-attempt timeout plus a few retries turns a
        frequent failure into a reliably fast success. The hosts are tried in
        turn because on a mainland China server the global host may be
        unreachable for an extended period while the China host still answers.

        Args:
            params: Query parameters for the search endpoint.
            what: Human readable name of the listing, used in errors.

        Returns:
            The decoded response body.

        Raises:
            SteamApiError: If every attempt on every host failed.
        """
        last_error = "unknown"
        for base in STEAM_SEARCH_BASES:
            url = base + STEAM_SEARCH_PATH
            for _ in range(_SPECIALS_ATTEMPTS):
                try:
                    response = await self.client.get(
                        url,
                        params=params,
                        timeout=_SPECIALS_ATTEMPT_TIMEOUT,
                    )
                    response.raise_for_status()
                    return response.json()
                except Exception as exc:  # noqa: BLE001 - retried below
                    last_error = _describe_error(exc)
        raise SteamApiError(
            f"{what}请求失败：{last_error}"
            f"（已在 {len(STEAM_SEARCH_BASES)} 个站点各重试 {_SPECIALS_ATTEMPTS} 次）"
        )

    async def _request_specials_page(
        self,
        country: str,
        start: int,
        count: int,
    ) -> dict[str, Any]:
        """Request one page of the specials list.

        Args:
            country: Steam storefront country code.
            start: Pagination offset.
            count: Rows to request.

        Returns:
            The decoded response body.

        Raises:
            SteamApiError: If every attempt failed.
        """
        params = {
            "query": "",
            "start": start,
            "count": count,
            "specials": 1,
            "infinite": 1,
            "cc": country,
            "l": self.language,
        }
        return await self._search_json(params, "Steam 特惠列表")


class HeyboxClient:
    """Client for the public Heybox endpoints used for name lookup and history."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        """Store the shared HTTP client.

        Args:
            client: Shared async HTTP client.
        """
        self.client = client

    async def search(self, query: str) -> list[GameCandidate]:
        """Search Heybox for a game name.

        Unlike the Steam storefront search this endpoint understands Chinese
        names, so it is what lets ``泰拉瑞亚`` resolve to appid 105600.

        Args:
            query: Raw user supplied game name.

        Returns:
            Candidate games. Console and mobile entries are dropped, as are
            soundtracks, DLC and demos which are never what the user meant.

        Raises:
            SteamApiError: If the endpoint fails.
        """
        try:
            response = await self.client.get(
                HEYBOX_SEARCH_URL,
                params={"q": query},
                headers={"Referer": f"{HEYBOX_WEB_BASE}/"},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            raise SteamApiError(f"小黑盒搜索请求失败：{_describe_error(exc)}") from exc

        games = (body.get("result") or {}).get("games") or []
        candidates: list[GameCandidate] = []
        for game in games:
            if not isinstance(game, dict):
                continue
            appid = game.get("steam_appid")
            name = str(game.get("name") or "").strip()
            # Heybox reports `type: "game"` plus `game_type: "pc"|"console"|...`.
            # Only PC entries map onto a Steam appid we can price.
            if game.get("game_type") != "pc" or not isinstance(appid, int) or not name:
                continue
            # Drop soundtracks, DLC, demos and playtests; the user wants the game.
            if game.get("type") not in (None, "game"):
                continue
            candidates.append(
                GameCandidate(
                    appid=appid,
                    name=name,
                    source="heybox",
                    popularity=int(game.get("follow_num") or 0),
                )
            )
        return candidates

    async def lowest_price(self, appid: int, country: str = "cn") -> LowestPrice | None:
        """Fetch the all time lowest price for an appid.

        Args:
            appid: Steam application id.
            country: Heybox region code.

        Returns:
            The lowest recorded price, or None when unavailable.

        Raises:
            SteamApiError: If the endpoint fails.
        """
        try:
            response = await self.client.get(
                HEYBOX_HISTORY_URL,
                params={"appid": appid, "platf": "steam", "cc": country, "days": 99999},
                headers={"Referer": f"{HEYBOX_WEB_BASE}/app/topic/game/pc/{appid}"},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            raise SteamApiError(f"小黑盒历史价格请求失败：{_describe_error(exc)}") from exc

        result = body.get("result") or {}
        info = result.get("lowest_info") or {}
        value = to_decimal(info.get("price"))
        if value is None:
            return None
        currency = str((result.get("lowest_info_v2") or {}).get("currency") or "").strip()
        return LowestPrice(
            value=value,
            currency=currency,
            recorded_on=_format_history_date(info.get("date")),
            discount_percent=int(info.get("discount") or 0),
        )


class SteamSearchClient:
    """Fallback name resolver backed by the Steam storefront search.

    This exists only to cover a Heybox outage. Steam's own search does not
    understand Chinese (``泰拉瑞亚`` returns nothing), so it is no replacement
    for Heybox, but it still resolves English names and therefore keeps the
    plugin usable in some form while Heybox is down.
    """

    def __init__(self, client: httpx.AsyncClient, language: str = "schinese") -> None:
        """Store the shared HTTP client.

        Args:
            client: Shared async HTTP client.
            language: Storefront language used for the returned titles.
        """
        self.client = client
        self.language = language

    async def search(self, query: str) -> list[GameCandidate]:
        """Search the Steam storefront for a game name.

        Args:
            query: Raw user supplied game name.

        Returns:
            Candidate games, best match first.

        Raises:
            SteamApiError: If the endpoint fails.
        """
        try:
            response = await self.client.get(
                STEAM_SEARCH_SUGGEST_URL,
                params={"term": query, "l": self.language, "cc": "CN"},
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
            raise SteamApiError(f"Steam 搜索请求失败：{_describe_error(exc)}") from exc

        # The endpoint answers {"total": n, "items": [{"id", "name", ...}]}.
        entries = body.get("items") if isinstance(body, dict) else body
        candidates: list[GameCandidate] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            # Accept either spelling: only `id` is documented, but the sibling
            # endpoints use `appid`.
            appid = entry.get("id") or entry.get("appid")
            name = str(entry.get("name") or "").strip()
            if not isinstance(appid, int) or isinstance(appid, bool) or not name:
                continue
            candidates.append(GameCandidate(appid=appid, name=name, source="steam"))
        return candidates


async def download_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Download an image, returning None instead of raising on failure.

    Args:
        client: Shared async HTTP client.
        url: Absolute image URL.

    Returns:
        The raw image bytes, or None when the download failed.
    """
    if not url:
        return None
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.content
    except Exception:  # noqa: BLE001 - a missing image must not break the card
        return None


def parse_explore_tab_items(page: str) -> list[dict[str, Any]]:
    """Extract game rows from the ``/explore/new/`` page.

    Every attribute sits on the opening ``<a>`` tag, so the whole tag has to be
    matched rather than just the element body. The page ships both the 热门新品
    and 新品 tabs in one document; they are returned in document order, which is
    the order the store displays them in.

    Args:
        page: Raw HTML of the explore page.

    Returns:
        Row dicts with appid, name, capsule, discount and price text.
    """
    rows: list[dict[str, Any]] = []
    for match in _TAB_ITEM_RE.finditer(page):
        tag, body = match.group(0), match.group(1)
        appid_match = _APPID_RE.search(tag)
        name_match = _TAB_NAME_RE.search(body)
        if not appid_match or not name_match:
            continue
        # Bundles carry a comma separated appid list and have no single price.
        if "," in appid_match.group(1):
            continue
        capsule_match = _TAB_CAP_RE.search(body)
        discount_match = _TAB_DISCOUNT_RE.search(body)
        final_match = _FINAL_RE.search(body)
        original_match = _ORIGINAL_RE.search(body)
        rows.append(
            {
                "appid": int(appid_match.group(1)),
                "name": html.unescape(name_match.group(1)).strip(),
                "capsule": capsule_match.group(1) if capsule_match else "",
                "discount": int(discount_match.group(1)) if discount_match else 0,
                "final": html.unescape(final_match.group(1)).strip() if final_match else "",
                "original": html.unescape(original_match.group(1)).strip()
                if original_match
                else "",
            }
        )
    return rows


def _parse_specials_page(results_html: str) -> list[dict[str, Any]]:
    """Extract listing rows from the infinite-scroll search payload.

    A discount is optional: the specials page only ever lists discounted
    entries, but the same markup is reused for rankings such as popular new
    releases, where most games are at full price.

    Args:
        results_html: The ``results_html`` field of the search response.

    Returns:
        Row dicts with appid, name, capsule, discount and price text.
    """
    rows: list[dict[str, Any]] = []
    for match in _ROW_RE.finditer(results_html):
        row = match.group(0)
        appid_match = _APPID_RE.search(row)
        if not appid_match:
            continue
        raw_appid = appid_match.group(1)
        # Bundles list several appids; they have no single price to show.
        if "," in raw_appid:
            continue
        title_match = _TITLE_RE.search(row)
        if not title_match:
            continue
        discount_match = _DISCOUNT_RE.search(row)
        final_match = _FINAL_RE.search(row)
        original_match = _ORIGINAL_RE.search(row)
        capsule_match = _CAPSULE_RE.search(row)
        rows.append(
            {
                "appid": int(raw_appid),
                "name": html.unescape(title_match.group(1)).strip(),
                "discount": int(discount_match.group(1)) if discount_match else 0,
                "final": html.unescape(final_match.group(1)).strip() if final_match else "",
                "original": html.unescape(original_match.group(1)).strip()
                if original_match
                else "",
                "capsule": capsule_match.group(1) if capsule_match else "",
            }
        )
    return rows


def parse_price(item: dict[str, Any]) -> PriceInfo | None:
    """Build a :class:`PriceInfo` from a store item payload.

    Args:
        item: One ``store_items`` entry.

    Returns:
        The parsed price, or None for free or unpriced items.
    """
    option = item.get("best_purchase_option") or {}
    current_text = str(option.get("formatted_final_price") or "").strip()
    if not current_text:
        return None
    original_text = str(option.get("formatted_original_price") or "").strip()
    discount = int(option.get("discount_pct") or 0)
    return PriceInfo(
        formatted_current=current_text,
        formatted_original=original_text or current_text,
        discount_percent=discount,
        current_value=_cents_to_decimal(option.get("final_price_in_cents")),
        original_value=_cents_to_decimal(option.get("original_price_in_cents")),
        discount_end=_parse_discount_end(option.get("active_discounts")),
    )


def parse_reviews(item: dict[str, Any]) -> ReviewSummary | None:
    """Build a :class:`ReviewSummary` from a store item payload.

    Args:
        item: One ``store_items`` entry.

    Returns:
        The review summary, or None when Steam reports no reviews.
    """
    summary = (item.get("reviews") or {}).get("summary_filtered") or {}
    count = int(summary.get("review_count") or 0)
    label = str(summary.get("review_score_label") or "").strip()
    if not count and not label:
        return None
    return ReviewSummary(
        label=label or "暂无评价",
        percent_positive=int(summary.get("percent_positive") or 0),
        review_count=count,
    )


def capsule_urls(item: dict[str, Any]) -> tuple[str, ...]:
    """Build candidate capsule image URLs for a store item, best first.

    The global API reports a ``main_capsule`` filename while the China API only
    reports the asset URL template, so Steam's fixed capsule paths are appended
    as fallbacks. Trying them in order covers games whose art does not sit at
    the standard path and would otherwise render as a placeholder.

    Args:
        item: One ``store_items`` entry.

    Returns:
        Absolute image URLs in preference order; empty when none can be built.
    """
    assets = item.get("assets") or {}
    template = str(assets.get("asset_url_format") or "")
    if not template:
        appid = item.get("appid")
        if not isinstance(appid, int) or appid <= 0:
            return ()
        template = f"steam/apps/{appid}/${{FILENAME}}"

    names: list[str] = []
    for key in ("main_capsule", "header", "small_capsule"):
        name = str(assets.get(key) or "").strip()
        if name and name not in names:
            names.append(name)
    for name in STEAM_CAPSULE_FALLBACKS:
        if name not in names:
            names.append(name)
    return tuple(STEAM_ASSET_BASE + template.replace("${FILENAME}", name) for name in names)


def capsule_url(item: dict[str, Any]) -> str:
    """Build the preferred capsule image URL for a store item.

    Args:
        item: One ``store_items`` entry.

    Returns:
        The best absolute image URL, or an empty string when none can be built.
    """
    urls = capsule_urls(item)
    return urls[0] if urls else ""


def _describe_error(exc: BaseException) -> str:
    """Describe an exception, which may stringify to nothing.

    httpx timeout errors carry no message, which previously produced a user
    facing error that ended in a bare colon.

    Args:
        exc: The exception to describe.

    Returns:
        The exception message, or its class name when the message is empty.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


def build_game_card(
    appid: int,
    item: dict[str, Any],
    lowest: LowestPrice | None = None,
) -> GameCard:
    """Assemble a :class:`GameCard` from a store item payload.

    Args:
        appid: Steam application id.
        item: The matching ``store_items`` entry.
        lowest: Optional all time lowest price.

    Returns:
        The assembled card.
    """
    basic = item.get("basic_info") or {}
    release = item.get("release") or {}
    return GameCard(
        appid=appid,
        name=str(item.get("name") or f"appid={appid}").strip(),
        price=parse_price(item),
        reviews=parse_reviews(item),
        capsule_urls=capsule_urls(item),
        lowest=lowest,
        release_date=_format_release_date(release.get("steam_release_date")),
        developers=_people_names(basic.get("developers")),
        short_description=str(basic.get("short_description") or "").strip(),
        is_free=parse_price(item) is None and not item.get("best_purchase_option"),
    )


def build_deal_item(
    row: dict[str, Any],
    item: dict[str, Any] | None,
    lowest: LowestPrice | None = None,
    allow_free: bool = False,
) -> DealItem | None:
    """Assemble a :class:`DealItem`, preferring enriched store data.

    Args:
        row: A parsed specials list row.
        item: The matching store item, when the batch lookup succeeded.
        lowest: Optional all time lowest price.
        allow_free: Keep entries with no price. Rankings need this, because
            the most popular games are frequently free to play and dropping
            them would misrepresent the list. The specials list leaves it off,
            since a discount listing without a price is meaningless.

    Returns:
        The assembled deal item, or None when no price could be determined.
    """
    price = parse_price(item) if item else None
    if price is None:
        # Fall back to the listing row, which still carries display prices.
        price = PriceInfo(
            formatted_current=row["final"],
            formatted_original=row["original"] or row["final"],
            discount_percent=row["discount"],
        )
    no_price = not price.formatted_current
    if no_price and not allow_free:
        return None
    if no_price:
        price = PriceInfo(
            formatted_current="免费游玩",
            formatted_original="免费游玩",
            discount_percent=0,
        )
    return DealItem(
        appid=row["appid"],
        name=(str(item.get("name")) if item and item.get("name") else row["name"]).strip(),
        price=price,
        capsule_urls=capsule_urls(item) if item else ((row["capsule"],) if row["capsule"] else ()),
        lowest=lowest,
        reviews=parse_reviews(item) if item else None,
    )


def _cents_to_decimal(value: Any) -> Any:
    """Convert a Steam cents string into a Decimal amount.

    Args:
        value: Price in cents, as a string or number.

    Returns:
        The amount as a Decimal, or None when the value is not numeric.
    """
    cents = to_decimal(value)
    return None if cents is None else cents / 100


def _parse_discount_end(active_discounts: Any) -> datetime | None:
    """Read the earliest discount end timestamp from a purchase option.

    Args:
        active_discounts: The ``active_discounts`` list of a purchase option.

    Returns:
        The end time in UTC, or None when no discount is scheduled to end.
    """
    if not isinstance(active_discounts, list):
        return None
    stamps = [
        int(entry["discount_end_date"])
        for entry in active_discounts
        if isinstance(entry, dict) and entry.get("discount_end_date")
    ]
    if not stamps:
        return None
    return datetime.fromtimestamp(min(stamps), tz=timezone.utc)


def _format_release_date(value: Any) -> str:
    """Format a Steam release timestamp as a date string.

    Args:
        value: Unix timestamp from the store item payload.

    Returns:
        An ISO date string, or an empty string when unavailable.
    """
    stamp = to_decimal(value)
    if stamp is None or stamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(stamp), tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _format_history_date(value: Any) -> str:
    """Normalise a Heybox lowest-price date into an ISO date string.

    Args:
        value: Either an ISO date string or a Unix timestamp.

    Returns:
        An ISO date string, or the original text when it cannot be parsed.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    stamp = to_decimal(text)
    if stamp is not None and stamp > 0:
        try:
            return datetime.fromtimestamp(float(stamp), tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def _people_names(value: Any) -> tuple[str, ...]:
    """Extract names from Steam developer or publisher lists.

    Args:
        value: The ``developers`` or ``publishers`` field.

    Returns:
        A tuple of non-empty names.
    """
    if not isinstance(value, list):
        return ()
    return tuple(
        str(entry.get("name")).strip()
        for entry in value
        if isinstance(entry, dict) and str(entry.get("name") or "").strip()
    )

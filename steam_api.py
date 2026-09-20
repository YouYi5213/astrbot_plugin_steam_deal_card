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
STEAM_SEARCH_RESULTS_URL = "https://store.steampowered.com/search/results/"
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
        last_error = "unknown"
        for _ in range(_SPECIALS_ATTEMPTS):
            try:
                response = await self.client.get(
                    STEAM_SEARCH_RESULTS_URL,
                    params=params,
                    timeout=_SPECIALS_ATTEMPT_TIMEOUT,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = _describe_error(exc)
        raise SteamApiError(
            f"Steam 特惠列表请求失败：{last_error}（已重试 {_SPECIALS_ATTEMPTS} 次）"
        )


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


def _parse_specials_page(results_html: str) -> list[dict[str, Any]]:
    """Extract discounted rows from the infinite-scroll search payload.

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
        if not discount_match:
            continue
        final_match = _FINAL_RE.search(row)
        if not final_match:
            continue
        original_match = _ORIGINAL_RE.search(row)
        capsule_match = _CAPSULE_RE.search(row)
        rows.append(
            {
                "appid": int(raw_appid),
                "name": html.unescape(title_match.group(1)).strip(),
                "discount": int(discount_match.group(1)),
                "final": html.unescape(final_match.group(1)).strip(),
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
) -> DealItem | None:
    """Assemble a :class:`DealItem`, preferring enriched store data.

    Args:
        row: A parsed specials list row.
        item: The matching store item, when the batch lookup succeeded.
        lowest: Optional all time lowest price.

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
    if not price.formatted_current:
        return None
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

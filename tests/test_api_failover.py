"""Tests for the Steam API host failover and asset fallbacks.

These cover the mainland-China deployment problem: ``api.steampowered.com``
hangs there while ``api.steamchina.com`` answers instantly, and the China host
omits the ``main_capsule`` asset key the global one provides.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_steam_deal_card.steam_api import (  # noqa: E402
    _HOST_PROBE_TIMEOUT,
    _SPECIALS_ATTEMPT_TIMEOUT,
    _SPECIALS_ATTEMPTS,
    STEAM_API_BASES,
    STEAM_CAPSULE_FALLBACKS,
    STEAM_GET_ITEMS_PATH,
    STEAM_MOST_PLAYED_PATH,
    STEAM_PLAYER_COUNT_PATH,
    STEAM_SEARCH_BASES,
    SteamApiError,
    SteamStoreClient,
    _describe_error,
    capsule_url,
    capsule_urls,
)


class _FakeResponse:
    """Minimal stand-in for an httpx response."""

    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=None, response=None)

    def json(self):
        return self._payload


class _ScriptedClient:
    """HTTP client stub that fails or answers per host."""

    def __init__(self, behaviour: dict[str, object]) -> None:
        """Record the behaviour mapping.

        Args:
            behaviour: host -> Exception instance or response payload.
        """
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def get(self, url: str, params=None, **kwargs):
        """Return or raise according to the scripted behaviour.

        Args:
            url: Full request URL.
            params: Ignored query parameters.
            **kwargs: Ignored request options, such as a per-request timeout.

        Returns:
            The scripted response.

        Raises:
            Exception: The scripted exception for that host.
        """
        host = url.split(STEAM_GET_ITEMS_PATH)[0]
        self.calls.append(host)
        result = self.behaviour.get(host, httpx.ConnectTimeout("unreachable"))
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


def _body(appid: int = 105600, name: str = "Terraria") -> dict:
    """Build a minimal GetItems response body.

    Args:
        appid: Application id to report.
        name: Display name to report.

    Returns:
        The response body.
    """
    return {"response": {"store_items": [{"appid": appid, "name": name, "success": 1}]}}


class FailoverTests(unittest.TestCase):
    def test_first_host_is_preferred_when_it_answers(self) -> None:
        client = _ScriptedClient({STEAM_API_BASES[0]: _body()})
        store = SteamStoreClient(client)
        items = asyncio.run(store.get_items([105600]))
        self.assertIn(105600, items)
        self.assertEqual(client.calls, [STEAM_API_BASES[0]])
        self.assertEqual(store._api_base, STEAM_API_BASES[0])

    def test_falls_over_to_the_second_host(self) -> None:
        client = _ScriptedClient(
            {
                STEAM_API_BASES[0]: httpx.ConnectTimeout("blocked"),
                STEAM_API_BASES[1]: _body(),
            }
        )
        store = SteamStoreClient(client)
        items = asyncio.run(store.get_items([105600]))
        self.assertIn(105600, items)
        self.assertEqual(client.calls, list(STEAM_API_BASES))
        self.assertEqual(store._api_base, STEAM_API_BASES[1])

    def test_working_host_is_remembered(self) -> None:
        client = _ScriptedClient(
            {
                STEAM_API_BASES[0]: httpx.ConnectTimeout("blocked"),
                STEAM_API_BASES[1]: _body(),
            }
        )
        store = SteamStoreClient(client)
        asyncio.run(store.get_items([105600]))
        client.calls.clear()
        asyncio.run(store.get_items([105600]))
        # The blocked host must not be probed again.
        self.assertEqual(client.calls, [STEAM_API_BASES[1]])

    def test_cached_host_is_dropped_when_it_stops_answering(self) -> None:
        client = _ScriptedClient({STEAM_API_BASES[1]: _body()})
        store = SteamStoreClient(client)
        asyncio.run(store.get_items([105600]))
        self.assertEqual(store._api_base, STEAM_API_BASES[1])

        # The previously good host now fails; the other one recovers.
        client.behaviour = {
            STEAM_API_BASES[0]: _body(),
            STEAM_API_BASES[1]: httpx.ReadTimeout("gone"),
        }
        asyncio.run(store.get_items([105600]))
        self.assertEqual(store._api_base, STEAM_API_BASES[0])

    def test_all_hosts_failing_raises_with_host_names(self) -> None:
        client = _ScriptedClient(
            {
                STEAM_API_BASES[0]: httpx.ConnectTimeout(""),
                STEAM_API_BASES[1]: httpx.ConnectTimeout(""),
            }
        )
        store = SteamStoreClient(client)
        with self.assertRaises(SteamApiError) as ctx:
            asyncio.run(store.get_items([105600]))
        message = str(ctx.exception)
        # Both hosts and a readable reason must be reported.
        self.assertIn(STEAM_API_BASES[0], message)
        self.assertIn(STEAM_API_BASES[1], message)
        self.assertIn("ConnectTimeout", message)

    def test_http_error_status_also_fails_over(self) -> None:
        client = _ScriptedClient(
            {
                STEAM_API_BASES[0]: _body(),
                STEAM_API_BASES[1]: _body(),
            }
        )

        async def failing_get(url, params=None, **kwargs):
            host = url.split(STEAM_GET_ITEMS_PATH)[0]
            client.calls.append(host)
            if host == STEAM_API_BASES[0]:
                return _FakeResponse(None, status=503)
            return _FakeResponse(_body())

        client.get = failing_get  # type: ignore[method-assign]
        store = SteamStoreClient(client)
        items = asyncio.run(store.get_items([105600]))
        self.assertIn(105600, items)
        self.assertEqual(store._api_base, STEAM_API_BASES[1])

    def test_chunks_use_the_resolved_host(self) -> None:
        client = _ScriptedClient({STEAM_API_BASES[1]: _body()})
        client.behaviour[STEAM_API_BASES[0]] = httpx.ConnectTimeout("blocked")
        store = SteamStoreClient(client)
        asyncio.run(store.get_items(list(range(1, 120))))
        # First chunk probes both hosts, the rest only the working one.
        self.assertEqual(client.calls.count(STEAM_API_BASES[0]), 1)
        self.assertEqual(client.calls.count(STEAM_API_BASES[1]), 3)

    def test_empty_appid_list_makes_no_request(self) -> None:
        client = _ScriptedClient({})
        store = SteamStoreClient(client)
        self.assertEqual(asyncio.run(store.get_items([])), {})
        self.assertEqual(client.calls, [])


class CapsuleUrlTests(unittest.TestCase):
    def test_uses_the_reported_filename_when_present(self) -> None:
        url = capsule_url(
            {
                "appid": 105600,
                "assets": {
                    "asset_url_format": "steam/apps/105600/${FILENAME}?t=1",
                    "main_capsule": "capsule_616x353.jpg",
                },
            }
        )
        self.assertTrue(url.endswith("steam/apps/105600/capsule_616x353.jpg?t=1"))

    def test_falls_back_to_the_fixed_path_without_a_filename(self) -> None:
        # The China API omits main_capsule/header but still sends the template.
        url = capsule_url(
            {"appid": 105600, "assets": {"asset_url_format": "steam/apps/105600/${FILENAME}?t=1"}}
        )
        self.assertTrue(url.endswith(f"steam/apps/105600/{STEAM_CAPSULE_FALLBACKS[0]}?t=1"))

    def test_falls_back_without_a_template_either(self) -> None:
        url = capsule_url({"appid": 105600, "assets": {}})
        self.assertTrue(url.endswith("steam/apps/105600/capsule_616x353.jpg"))

    def test_returns_empty_without_an_appid(self) -> None:
        self.assertEqual(capsule_url({"assets": {}}), "")
        self.assertEqual(capsule_url({"appid": 0, "assets": {}}), "")

    def test_header_is_used_when_main_capsule_is_absent(self) -> None:
        url = capsule_url(
            {
                "appid": 1,
                "assets": {"asset_url_format": "steam/apps/1/${FILENAME}", "header": "h.jpg"},
            }
        )
        self.assertTrue(url.endswith("steam/apps/1/h.jpg"))


class CapsuleUrlListTests(unittest.TestCase):
    def test_reports_candidates_in_preference_order(self) -> None:
        urls = capsule_urls(
            {
                "appid": 105600,
                "assets": {
                    "asset_url_format": "steam/apps/105600/${FILENAME}?t=1",
                    "main_capsule": "reported.jpg",
                },
            }
        )
        # The reported filename wins, then the fixed fallbacks follow.
        self.assertEqual(urls[0].split("/")[-1], "reported.jpg?t=1")
        self.assertTrue(any(url.endswith("capsule_616x353.jpg?t=1") for url in urls))
        self.assertTrue(any(url.endswith("header.jpg?t=1") for url in urls))

    def test_always_offers_fallbacks_for_the_china_api_shape(self) -> None:
        # This is the shape that produced a 404 in production.
        urls = capsule_urls(
            {"appid": 2909400, "assets": {"asset_url_format": "steam/apps/2909400/${FILENAME}?t=2"}}
        )
        self.assertGreaterEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("capsule_616x353.jpg?t=2"))
        self.assertTrue(urls[1].endswith("header.jpg?t=2"))

    def test_candidates_are_deduplicated(self) -> None:
        urls = capsule_urls(
            {
                "appid": 1,
                "assets": {
                    "asset_url_format": "steam/apps/1/${FILENAME}",
                    "main_capsule": "capsule_616x353.jpg",
                },
            }
        )
        self.assertEqual(len(urls), len(set(urls)))

    def test_returns_empty_tuple_without_an_appid_or_template(self) -> None:
        self.assertEqual(capsule_urls({"assets": {}}), ())
        self.assertEqual(capsule_urls({"appid": 0, "assets": {}}), ())

    def test_all_candidates_share_the_same_template(self) -> None:
        urls = capsule_urls(
            {"appid": 7, "assets": {"asset_url_format": "steam/apps/7/${FILENAME}?t=9"}}
        )
        self.assertTrue(
            all(url.startswith("https://shared.akamai.steamstatic.com/") for url in urls)
        )
        self.assertTrue(all(url.endswith("?t=9") for url in urls))


class ProbeTimeoutTests(unittest.TestCase):
    def test_unconfirmed_host_is_probed_with_a_short_timeout(self) -> None:
        seen: list[object] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                seen.append(kwargs.get("timeout"))
                return _FakeResponse(_body())

        store = SteamStoreClient(_Client())
        asyncio.run(store.get_items([105600]))
        self.assertEqual(seen, [_HOST_PROBE_TIMEOUT])

    def test_confirmed_host_uses_the_client_default_timeout(self) -> None:
        seen: list[object] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                seen.append(kwargs.get("timeout"))
                return _FakeResponse(_body())

        store = SteamStoreClient(_Client())
        asyncio.run(store.get_items([105600]))
        asyncio.run(store.get_items([105600]))
        # First call probes, second trusts the cached host and passes no override.
        self.assertEqual(seen, [_HOST_PROBE_TIMEOUT, None])


class SpecialsRetryTests(unittest.TestCase):
    """The specials host refuses connections intermittently, not permanently."""

    PAGE = {
        "results_html": (
            '<a href="x" data-ds-appid="2369390" class="search_result_row">'
            '<div class="search_capsule"><img src="https://i/x.jpg"></div>'
            '<span class="title">Far Cry 6</span>'
            '<div class="discount_block" data-discount="90">'
            '<div class="discount_original_price">\u00a5298.00</div>'
            '<div class="discount_final_price">\u00a529.80</div></div></a>'
        )
    }

    def _client(self, failures_before_success: int):
        """Build a client that refuses the first N attempts.

        Args:
            failures_before_success: How many attempts raise before one succeeds.

        Returns:
            A tuple of (client stub, call log).
        """
        calls: list[object] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                calls.append(kwargs.get("timeout"))
                if len(calls) <= failures_before_success:
                    # Real httpx timeouts carry an empty message.
                    raise httpx.ConnectTimeout("")
                return _FakeResponse(SpecialsRetryTests.PAGE)

        return _Client(), calls

    def test_recovers_after_transient_failures(self) -> None:
        client, calls = self._client(failures_before_success=3)
        rows = asyncio.run(SteamStoreClient(client).specials("CN", limit=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["appid"], 2369390)
        self.assertEqual(len(calls), 4)

    def test_eventual_failure_reports_the_attempt_count(self) -> None:
        client, calls = self._client(failures_before_success=99)
        with self.assertRaises(SteamApiError) as ctx:
            asyncio.run(SteamStoreClient(client).specials("CN", limit=1))
        self.assertIn(str(_SPECIALS_ATTEMPTS), str(ctx.exception))
        self.assertIn("ConnectTimeout", str(ctx.exception))
        # Both storefront hosts are tried before giving up.
        self.assertEqual(len(calls), _SPECIALS_ATTEMPTS * len(STEAM_SEARCH_BASES))

    def test_the_search_endpoint_fails_over_to_the_second_host(self) -> None:
        # The global storefront is unreachable from some regions for extended
        # periods, while the China host still answers with the same envelope.
        hosts: list[str] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                hosts.append(str(url))
                if "store.steampowered.com" in str(url):
                    raise httpx.ConnectTimeout("")
                return _FakeResponse(SpecialsRetryTests.PAGE)

        rows = asyncio.run(SteamStoreClient(_Client()).specials("CN", limit=1))
        self.assertEqual(len(rows), 1)
        self.assertTrue(hosts[0].startswith("https://store.steampowered.com"))
        self.assertTrue(
            any(h.startswith("https://store.steamchina.com") for h in hosts),
            "should have retried on the China storefront host",
        )

    def test_the_global_host_is_preferred_when_it_answers(self) -> None:
        # The China host carries a far smaller catalogue, so it must never be
        # used while the global host is healthy.
        hosts: list[str] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                hosts.append(str(url))
                return _FakeResponse(SpecialsRetryTests.PAGE)

        asyncio.run(SteamStoreClient(_Client()).specials("CN", limit=1))
        self.assertTrue(all("store.steamchina.com" not in h for h in hosts))

    def test_every_attempt_uses_the_short_budget(self) -> None:
        client, calls = self._client(failures_before_success=1)
        asyncio.run(SteamStoreClient(client).specials("CN", limit=1))
        self.assertTrue(all(t is _SPECIALS_ATTEMPT_TIMEOUT for t in calls))

    def test_success_on_first_attempt_does_not_retry(self) -> None:
        client, calls = self._client(failures_before_success=0)
        asyncio.run(SteamStoreClient(client).specials("CN", limit=1))
        self.assertEqual(len(calls), 1)

    def test_empty_page_stops_pagination(self) -> None:
        calls: list[int] = []

        class _Client:
            async def get(self, url, params=None, **kwargs):
                calls.append(params["start"])
                if params["start"] == 0:
                    return _FakeResponse(SpecialsRetryTests.PAGE)
                return _FakeResponse({"results_html": ""})

        rows = asyncio.run(SteamStoreClient(_Client()).specials("CN", limit=5))
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls, [0, 50])


class PlayerCountApiTests(unittest.TestCase):
    """The two player-count endpoints, including their failover behaviour."""

    def _client(self, payload, calls: list | None = None, fail_on: set | None = None):
        log = calls if calls is not None else []
        failing = fail_on or set()

        class _Client:
            async def get(self, url, params=None, **kwargs):
                log.append((url, params))
                if any(host in url for host in failing):
                    raise httpx.ConnectTimeout("")
                return _FakeResponse(payload)

        return _Client()

    def test_current_players_reads_the_count(self) -> None:
        client = self._client({"response": {"player_count": 498925, "result": 1}})
        store = SteamStoreClient(client)
        self.assertEqual(asyncio.run(store.current_players(730)), 498925)

    def test_current_players_hits_the_documented_path(self) -> None:
        calls: list = []
        client = self._client({"response": {"player_count": 1}}, calls)
        asyncio.run(SteamStoreClient(client).current_players(570))
        url, params = calls[0]
        self.assertTrue(url.endswith(STEAM_PLAYER_COUNT_PATH))
        self.assertEqual(params, {"appid": 570})

    def test_missing_count_is_none_not_zero(self) -> None:
        # Steam omits player_count for delisted apps; reporting 0 would read as
        # "nobody is playing" rather than "not published".
        client = self._client({"response": {"result": 42}})
        self.assertIsNone(asyncio.run(SteamStoreClient(client).current_players(1)))

    def test_non_integer_count_is_none(self) -> None:
        client = self._client({"response": {"player_count": "lots"}})
        self.assertIsNone(asyncio.run(SteamStoreClient(client).current_players(1)))

    def test_most_played_parses_ranks(self) -> None:
        payload = {
            "response": {
                "rollup_date": 1789776000,
                "ranks": [
                    {"rank": 1, "appid": 730, "peak_in_game": 1317931},
                    {"rank": 2, "appid": 570, "peak_in_game": 860350},
                ],
            }
        }
        rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played())
        self.assertEqual(
            rows,
            [
                {"appid": 730, "peak_in_game": 1317931, "rollup_date": 1789776000},
                {"appid": 570, "peak_in_game": 860350, "rollup_date": 1789776000},
            ],
        )

    def test_most_played_carries_the_rollup_date(self) -> None:
        # The peak describes one completed day, so the day has to survive
        # parsing; without it the card would imply the figure is today's.
        payload = {
            "response": {
                "rollup_date": 1789776000,
                "ranks": [{"appid": 730, "peak_in_game": 1}],
            }
        }
        rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played())
        self.assertEqual(rows[0]["rollup_date"], 1789776000)

    def test_most_played_tolerates_a_missing_or_bogus_rollup_date(self) -> None:
        for payload in (
            {"response": {"ranks": [{"appid": 730, "peak_in_game": 1}]}},
            {"response": {"rollup_date": "yesterday", "ranks": [{"appid": 730}]}},
            {"response": {"rollup_date": True, "ranks": [{"appid": 730}]}},
        ):
            with self.subTest(payload=payload):
                rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played())
                self.assertEqual(rows[0]["rollup_date"], 0)

    def test_most_played_honours_the_limit(self) -> None:
        payload = {"response": {"ranks": [{"appid": i, "peak_in_game": i} for i in range(1, 11)]}}
        rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played(3))
        self.assertEqual([row["appid"] for row in rows], [1, 2, 3])

    def test_most_played_drops_rows_without_an_appid(self) -> None:
        payload = {"response": {"ranks": [{"appid": 730}, {"peak_in_game": 5}, "junk"]}}
        rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played())
        self.assertEqual(rows, [{"appid": 730, "peak_in_game": 0, "rollup_date": 0}])

    def test_most_played_keeps_a_missing_peak_as_zero(self) -> None:
        payload = {"response": {"ranks": [{"appid": 730}, {"appid": 570, "peak_in_game": 9}]}}
        rows = asyncio.run(SteamStoreClient(self._client(payload)).most_played())
        self.assertEqual(rows[0]["peak_in_game"], 0)
        self.assertEqual(rows[1]["peak_in_game"], 9)

    def test_most_played_hits_the_documented_path(self) -> None:
        calls: list = []
        client = self._client({"response": {"ranks": []}}, calls)
        asyncio.run(SteamStoreClient(client).most_played())
        self.assertTrue(calls[0][0].endswith(STEAM_MOST_PLAYED_PATH))

    def test_player_count_fails_over_to_the_china_host(self) -> None:
        calls: list = []
        client = self._client(
            {"response": {"player_count": 7}}, calls, fail_on={STEAM_API_BASES[0]}
        )
        count = asyncio.run(SteamStoreClient(client).current_players(730))
        self.assertEqual(count, 7)
        self.assertEqual(len(calls), 2)
        self.assertIn(STEAM_API_BASES[1], calls[1][0])

    def test_player_count_raises_when_every_host_fails(self) -> None:
        client = self._client({}, fail_on=set(STEAM_API_BASES))
        with self.assertRaises(SteamApiError):
            asyncio.run(SteamStoreClient(client).current_players(730))

    def test_a_host_that_answers_later_is_remembered(self) -> None:
        calls: list = []
        client = self._client(
            {"response": {"player_count": 7}}, calls, fail_on={STEAM_API_BASES[0]}
        )
        store = SteamStoreClient(client)
        asyncio.run(store.current_players(730))
        asyncio.run(store.current_players(730))
        # Second call goes straight to the host that worked.
        self.assertIn(STEAM_API_BASES[1], calls[-1][0])
        self.assertEqual(len(calls), 3)


class DescribeErrorTests(unittest.TestCase):
    def test_uses_the_message_when_present(self) -> None:
        self.assertEqual(_describe_error(ValueError("boom")), "boom")

    def test_falls_back_to_the_class_name(self) -> None:
        # httpx timeouts stringify to nothing, which produced a bare colon.
        self.assertEqual(_describe_error(httpx.ReadTimeout("")), "ReadTimeout")
        self.assertEqual(_describe_error(httpx.ConnectTimeout("")), "ConnectTimeout")

    def test_blank_message_falls_back(self) -> None:
        self.assertEqual(_describe_error(ValueError("   ")), "ValueError")


if __name__ == "__main__":
    unittest.main()

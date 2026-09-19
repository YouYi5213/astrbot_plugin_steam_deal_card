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
    STEAM_API_BASES,
    STEAM_CAPSULE_FALLBACKS,
    STEAM_GET_ITEMS_PATH,
    SteamApiError,
    SteamStoreClient,
    _describe_error,
    capsule_url,
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

    async def get(self, url: str, params=None):
        """Return or raise according to the scripted behaviour.

        Args:
            url: Full request URL.
            params: Ignored query parameters.

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

        async def failing_get(url, params=None):
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

"""Tests for the Heybox outage fallback and the startup health probe.

Both cover the same concern from different angles: every upstream endpoint is
unofficial, so the plugin must degrade in a way the user can understand rather
than failing with a raw exception.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_steam_deal_card.health import (  # noqa: E402
    ProbeResult,
    probe_heybox,
    probe_players,
    probe_steam_chart,
    probe_steam_store,
    run_health_check,
)
from astrbot_plugin_steam_deal_card.models import GameCandidate, LowestPrice  # noqa: E402
from astrbot_plugin_steam_deal_card.service import (  # noqa: E402
    LookupError,
    SteamDealService,
)
from astrbot_plugin_steam_deal_card.steam_api import (  # noqa: E402
    STEAM_SEARCH_SUGGEST_URL,
    SteamApiError,
    SteamSearchClient,
    SteamStoreClient,
    build_deal_item,
)

# CJK written as escapes so the file survives a tool that rewrites encoding.
TERRARIA_CN = "\u6cf0\u62c9\u745e\u4e9a"  # 泰拉瑞亚
OUTAGE = "\u6682\u65f6\u65e0\u6cd5\u89e3\u6790"  # 暂时无法解析
HEYBOX_DOWN = "\u5c0f\u9ed1\u76d2"  # 小黑盒
FALLBACK_HINT = "105600"


class _FailingHeybox:
    """Heybox stub that always fails, simulating an outage."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query):  # noqa: ANN001, ANN201
        self.calls += 1
        raise SteamApiError("小黑盒搜索请求失败：连接超时")

    async def lowest_price(self, appid, country="cn"):  # noqa: ANN001, ANN201
        raise SteamApiError("小黑盒历史价格请求失败：连接超时")


class _WorkingHeybox:
    def __init__(self, candidates: list[GameCandidate]) -> None:
        self._candidates = candidates
        self.calls = 0

    async def search(self, query):  # noqa: ANN001, ANN201
        self.calls += 1
        return list(self._candidates)

    async def lowest_price(self, appid, country="cn"):  # noqa: ANN001, ANN201
        return None


class _EmptyHeybox(_WorkingHeybox):
    def __init__(self) -> None:
        super().__init__([])


class _SearchStub:
    """Steam storefront search stub."""

    def __init__(self, candidates=None, fail: bool = False) -> None:
        self._candidates = candidates or []
        self._fail = fail
        self.calls = 0

    async def search(self, query):  # noqa: ANN001, ANN201
        self.calls += 1
        if self._fail:
            raise SteamApiError("Steam 搜索请求失败：连接超时")
        return list(self._candidates)


class _StoreStub:
    """Store stub: only get_items matters for name resolution."""

    def __init__(self, items=None) -> None:
        self._items = items or {}

    async def get_items(self, appids, country):  # noqa: ANN001, ANN201
        return {appid: self._items[appid] for appid in appids if appid in self._items}

    async def most_played(self, limit: int = 100):  # noqa: ANN201
        return []

    async def current_players(self, appid: int):  # noqa: ANN201
        return None


def _service(heybox, search, items=None) -> SteamDealService:
    return SteamDealService(
        store=_StoreStub(items),  # type: ignore[arg-type]
        heybox=heybox,  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        country="CN",
        history_country="cn",
        search=search,  # type: ignore[arg-type]
    )


class HeyboxFallbackTests(unittest.TestCase):
    """Chinese resolution dies with Heybox, so the plugin must degrade clearly."""

    def test_heybox_is_preferred_when_it_works(self) -> None:
        heybox = _WorkingHeybox([GameCandidate(105600, "Terraria", score=0.0)])
        search = _SearchStub([GameCandidate(1, "Wrong")])
        service = _service(heybox, search, {105600: {"name": "Terraria"}})
        asyncio.run(service.resolve_game("Terraria"))
        self.assertEqual(heybox.calls, 1)
        self.assertEqual(search.calls, 0)

    def test_falls_back_to_steam_search_when_heybox_fails(self) -> None:
        heybox = _FailingHeybox()
        search = _SearchStub([GameCandidate(105600, "Terraria")])
        service = _service(heybox, search, {105600: {"name": "Terraria"}})
        result = asyncio.run(service.resolve_game("Terraria"))
        self.assertEqual(search.calls, 1)
        self.assertIsNotNone(result.card)
        self.assertEqual(result.card.appid, 105600)

    def test_chinese_name_gets_an_explained_error_when_heybox_is_down(self) -> None:
        # The fallback cannot resolve Chinese, so the message must say why and
        # offer a way forward instead of surfacing a raw network error.
        heybox = _FailingHeybox()
        search = _SearchStub([])  # Steam search returns nothing for Chinese
        service = _service(heybox, search)
        with self.assertRaises(LookupError) as ctx:
            asyncio.run(service.resolve_game(TERRARIA_CN))
        message = str(ctx.exception)
        self.assertIn(OUTAGE, message)
        self.assertIn(HEYBOX_DOWN, message)
        self.assertIn(FALLBACK_HINT, message)

    def test_error_mentions_a_usable_alternative(self) -> None:
        service = _service(_FailingHeybox(), _SearchStub([]))
        with self.assertRaises(LookupError) as ctx:
            asyncio.run(service.resolve_game(TERRARIA_CN))
        self.assertIn("steam游戏", str(ctx.exception))

    def test_both_sources_failing_still_explains_itself(self) -> None:
        service = _service(_FailingHeybox(), _SearchStub(fail=True))
        with self.assertRaises(LookupError) as ctx:
            asyncio.run(service.resolve_game(TERRARIA_CN))
        self.assertIn(OUTAGE, str(ctx.exception))

    def test_genuinely_unknown_name_keeps_the_plain_message(self) -> None:
        # A working Heybox that finds nothing is a different case from an
        # outage, and must not be reported as a broken service.
        service = _service(_EmptyHeybox(), _SearchStub([]))
        with self.assertRaises(LookupError) as ctx:
            asyncio.run(service.resolve_game("zzzznotagame"))
        message = str(ctx.exception)
        self.assertNotIn(HEYBOX_DOWN, message)
        self.assertIn("zzzznotagame", message)

    def test_fallback_candidates_with_no_name_match_are_rejected(self) -> None:
        # The fallback must not offer unrelated games as choices.
        search = _SearchStub([GameCandidate(999, "Something Else Entirely")])
        service = _service(_FailingHeybox(), search, {999: {"name": "Something Else Entirely"}})
        with self.assertRaises(LookupError) as ctx:
            asyncio.run(service.resolve_game(TERRARIA_CN))
        self.assertIn(OUTAGE, str(ctx.exception))

    def test_appid_path_never_touches_heybox(self) -> None:
        # An explicit appid must keep working during a Heybox outage.
        heybox = _FailingHeybox()
        service = _service(heybox, _SearchStub([]), {105600: {"name": "Terraria"}})
        result = asyncio.run(service.resolve_game("105600"))
        self.assertEqual(heybox.calls, 0)
        self.assertEqual(result.card.appid, 105600)

    def test_lowest_price_failure_does_not_break_the_card(self) -> None:
        # Heybox also serves history; its failure must only drop that line.
        service = _service(_WorkingHeybox([]), _SearchStub([]), {105600: {"name": "Terraria"}})
        card = asyncio.run(service.build_card(105600))
        self.assertIsNone(card.lowest)
        self.assertEqual(card.name, "Terraria")


class _FakeResponse:
    def __init__(self, payload, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._payload


class SteamSearchClientTests(unittest.TestCase):
    def _client(self, payload, calls: list | None = None):
        log = calls if calls is not None else []

        class _C:
            async def get(self, url, params=None, **kwargs):
                log.append((url, params))
                return _FakeResponse(payload)

        return _C()

    def test_parses_the_items_envelope(self) -> None:
        # The real endpoint answers {"total": n, "items": [...]}, not a bare list.
        payload = {
            "total": 2,
            "items": [{"id": 105600, "name": "Terraria"}, {"id": 570, "name": "Dota 2"}],
        }
        found = asyncio.run(SteamSearchClient(self._client(payload)).search("Terraria"))
        self.assertEqual([c.appid for c in found], [105600, 570])
        self.assertEqual(found[0].source, "steam")

    def test_accepts_a_bare_list_too(self) -> None:
        payload = [{"id": 105600, "name": "Terraria"}]
        found = asyncio.run(SteamSearchClient(self._client(payload)).search("Terraria"))
        self.assertEqual(found[0].appid, 105600)

    def test_accepts_the_appid_key_spelling(self) -> None:
        payload = {"items": [{"appid": 105600, "name": "Terraria"}]}
        found = asyncio.run(SteamSearchClient(self._client(payload)).search("Terraria"))
        self.assertEqual(found[0].appid, 105600)

    def test_skips_malformed_entries(self) -> None:
        payload = {
            "items": [
                {"id": 105600, "name": "Terraria"},
                {"id": "not-an-int", "name": "Bad"},
                {"id": 1},
                "junk",
                {"id": 2, "name": "   "},
                {"id": True, "name": "Bool"},
            ]
        }
        found = asyncio.run(SteamSearchClient(self._client(payload)).search("x"))
        self.assertEqual([c.appid for c in found], [105600])

    def test_a_non_dict_body_yields_no_candidates(self) -> None:
        found = asyncio.run(SteamSearchClient(self._client("nope")).search("x"))
        self.assertEqual(found, [])

    def test_hits_the_json_search_endpoint(self) -> None:
        calls: list = []
        asyncio.run(SteamSearchClient(self._client({"items": []}, calls)).search("x"))
        self.assertTrue(calls[0][0].endswith(STEAM_SEARCH_SUGGEST_URL))
        # The HTML suggest endpoint would have been a silent no-op.
        self.assertNotIn("/search/suggest", calls[0][0])

    def test_network_failure_becomes_a_domain_error(self) -> None:
        class _Boom:
            async def get(self, *a, **k):
                raise httpx.ConnectTimeout("")

        with self.assertRaises(SteamApiError):
            asyncio.run(SteamSearchClient(_Boom()).search("x"))


class _ProbeStore:
    def __init__(self, items=None, chart=None, players=None, fail=None) -> None:
        self._items = items or {}
        self._chart = chart or []
        self._players = players
        self._fail = fail or set()

    async def get_items(self, appids, country):  # noqa: ANN001, ANN201
        if "items" in self._fail:
            raise SteamApiError("连接超时")
        return self._items

    async def most_played(self, limit: int = 100):  # noqa: ANN201
        if "chart" in self._fail:
            raise SteamApiError("连接超时")
        return self._chart

    async def current_players(self, appid: int):  # noqa: ANN201
        if "players" in self._fail:
            raise SteamApiError("连接超时")
        return self._players


class _ProbeHeybox:
    def __init__(self, candidates=None, fail: bool = False) -> None:
        self._candidates = candidates or []
        self._fail = fail

    async def search(self, query):  # noqa: ANN001, ANN201
        if self._fail:
            raise SteamApiError("小黑盒搜索请求失败：连接超时")
        return list(self._candidates)


class HealthProbeTests(unittest.TestCase):
    def test_store_probe_passes_with_data(self) -> None:
        store = _ProbeStore(items={730: {"name": "CS2"}})
        store._api_base = "https://api.steamchina.com"
        result = asyncio.run(probe_steam_store(store))  # type: ignore[arg-type]
        self.assertTrue(result.ok)
        self.assertTrue(result.critical)
        self.assertIn("api.steamchina.com", result.detail)

    def test_store_probe_fails_when_empty(self) -> None:
        result = asyncio.run(probe_steam_store(_ProbeStore(items={})))  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIn("没有返回数据", result.detail)

    def test_store_probe_reports_the_error(self) -> None:
        store = _ProbeStore(fail={"items"})
        result = asyncio.run(probe_steam_store(store))  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertTrue(result.critical)

    def test_heybox_probe_passes(self) -> None:
        heybox = _ProbeHeybox([GameCandidate(105600, "Terraria")])
        result = asyncio.run(probe_heybox(heybox))  # type: ignore[arg-type]
        self.assertTrue(result.ok)
        self.assertIn("105600", result.detail)

    def test_heybox_probe_passes_with_no_results(self) -> None:
        # Reachable but empty is a working endpoint, not a broken one.
        result = asyncio.run(probe_heybox(_ProbeHeybox([])))  # type: ignore[arg-type]
        self.assertTrue(result.ok)

    def test_heybox_probe_fails_on_error(self) -> None:
        result = asyncio.run(probe_heybox(_ProbeHeybox(fail=True)))  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertTrue(result.critical)

    def test_chart_probe(self) -> None:
        ok = asyncio.run(
            probe_steam_chart(_ProbeStore(chart=[{"appid": 730, "peak_in_game": 1}]))  # type: ignore[arg-type]
        )
        self.assertTrue(ok.ok)
        empty = asyncio.run(probe_steam_chart(_ProbeStore(chart=[])))  # type: ignore[arg-type]
        self.assertFalse(empty.ok)

    def test_players_probe(self) -> None:
        ok = asyncio.run(probe_players(_ProbeStore(players=498925)))  # type: ignore[arg-type]
        self.assertTrue(ok.ok)
        missing = asyncio.run(probe_players(_ProbeStore(players=None)))  # type: ignore[arg-type]
        self.assertFalse(missing.ok)

    def test_run_health_check_returns_one_result_per_dependency(self) -> None:
        store = _ProbeStore(
            items={730: {"name": "CS2"}}, chart=[{"appid": 730, "peak_in_game": 1}], players=5
        )
        heybox = _ProbeHeybox([GameCandidate(105600, "Terraria")])
        results = asyncio.run(run_health_check(store, heybox))  # type: ignore[arg-type]
        self.assertEqual(len(results), 4)
        self.assertTrue(all(isinstance(r, ProbeResult) for r in results))

    def test_one_broken_dependency_does_not_hide_the_others(self) -> None:
        # Heybox down must not stop the Steam probes from reporting.
        store = _ProbeStore(
            items={730: {"name": "CS2"}}, chart=[{"appid": 730, "peak_in_game": 1}], players=5
        )
        results = asyncio.run(run_health_check(store, _ProbeHeybox(fail=True)))  # type: ignore[arg-type]
        by_name = {r.name: r for r in results}
        self.assertTrue(by_name["Steam 商店数据"].ok)
        self.assertFalse(by_name["小黑盒中文名转换"].ok)

    def test_health_check_is_bounded_by_its_timeout(self) -> None:
        # A hanging network must not delay startup indefinitely.
        class _Hanging:
            async def get_items(self, *a, **k):
                await asyncio.sleep(30)

            async def most_played(self, *a, **k):
                await asyncio.sleep(30)

            async def current_players(self, *a, **k):
                await asyncio.sleep(30)

        class _HangingHeybox:
            async def search(self, *a, **k):
                await asyncio.sleep(30)

        results = asyncio.run(
            run_health_check(_Hanging(), _HangingHeybox(), timeout=0.05)  # type: ignore[arg-type]
        )
        self.assertEqual(results, [])

    def test_health_check_never_raises_on_unexpected_errors(self) -> None:
        class _Boom:
            async def get_items(self, *a, **k):
                raise RuntimeError("unexpected")

            async def most_played(self, *a, **k):
                raise ValueError("unexpected")

            async def current_players(self, *a, **k):
                raise KeyError("unexpected")

        class _BoomHeybox:
            async def search(self, *a, **k):
                raise TypeError("unexpected")

        results = asyncio.run(run_health_check(_Boom(), _BoomHeybox()))  # type: ignore[arg-type]
        self.assertEqual(len(results), 4)
        self.assertTrue(all(not r.ok for r in results))

    def test_probe_results_are_logged(self) -> None:
        store = _ProbeStore(items={}, chart=[], players=None)
        with patch("astrbot_plugin_steam_deal_card.health.logger") as log:
            asyncio.run(run_health_check(store, _ProbeHeybox(fail=True)))  # type: ignore[arg-type]
        logged = " ".join(str(c) for c in log.warning.call_args_list)
        self.assertIn("接口自检", logged)

    def test_timeout_error_is_described_not_blank(self) -> None:
        # httpx timeouts stringify to "" which would log a bare colon.
        store = _ProbeStore(fail={"players"})
        original = store.current_players

        async def _blank_timeout(appid):  # noqa: ANN001, ANN202
            raise httpx.ReadTimeout("")

        store.current_players = _blank_timeout  # type: ignore[assignment]
        try:
            result = asyncio.run(probe_players(store))  # type: ignore[arg-type]
        finally:
            store.current_players = original  # type: ignore[assignment]
        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "ReadTimeout")


class HomeShelfTests(unittest.TestCase):
    """The home page's 热门即将推出 shelf.

    It is inline in the home page document, so no extra endpoint is involved,
    and it is the list the store itself shows when you scroll the home page.
    """

    # Markup mirrors the real page: tab_row_item rows, a lazy loaded capsule,
    # and a separate container per tab.
    PAGE = (
        '<div id="tab_newreleases_content">'
        '<a class="tab_row_item" data-ds-appid="3058360" href="x">'
        '<div class="tab_item_title">\u6cd5\u56fd\u5c0f\u9986\u513f</div></a>'
        "</div>"
        '<div id="tab_upcoming_content">'
        '<a class="tab_row_item" data-ds-appid="4705510" href="x">'
        '<img class="tab_row_capsule" src="https://cdn/trans.gif" '
        'data-delayed-image="https://cdn/4705510/c.jpg" alt="H">'
        '<div class="tab_item_title">Happy Wheels</div>'
        '<div class="tab_item_release_date">'
        "\u53d1\u884c\u65e5\u671f: 2026 \u5e74 9 \u6708 21 \u65e5</div>"
        '<div class="discount_block" data-discount="0">'
        '<div class="discount_final_price">\u00a568.00</div></div></a>'
        '<a class="tab_row_item" data-ds-appid="4358690" href="x">'
        '<div class="tab_item_title">\u5b88\u5893\u4eba2</div>'
        '<div class="tab_item_release_date">'
        "\u53d1\u884c\u65e5\u671f: 2026 \u5e74 9 \u6708 22 \u65e5</div></a>"
        "</div>"
        '<div id="tab_other_content"></div>'
    )

    def _client(self, page=None, boom=False):
        pages = self.PAGE if page is None else page

        class _C:
            def __init__(self):
                self.urls = []

            async def get(self, url, params=None, **kwargs):
                self.urls.append(str(url))
                if boom:
                    raise httpx.ConnectTimeout("")
                return _FakeResponse({}, text=pages)

        return _C()

    def test_upcoming_reads_the_home_page(self) -> None:
        client = self._client()
        rows = asyncio.run(SteamStoreClient(client).popular_upcoming("CN", 10))
        self.assertTrue(any(u.rstrip("/") == "https://store.steampowered.com" for u in client.urls))
        self.assertEqual([r["appid"] for r in rows], [4705510, 4358690])

    def test_upcoming_parses_capsule_price_and_release(self) -> None:
        rows = asyncio.run(SteamStoreClient(self._client()).popular_upcoming("CN", 10))
        first = rows[0]
        self.assertEqual(first["name"], "Happy Wheels")
        self.assertEqual(first["final"], "\u00a568.00")
        # The capsule is lazy loaded, so src is a placeholder and the real URL
        # lives in data-delayed-image.
        self.assertEqual(first["capsule"], "https://cdn/4705510/c.jpg")
        self.assertIn("2026", first["release"])
        self.assertIn("21", first["release"])

    def test_upcoming_reads_only_its_own_container(self) -> None:
        # The page carries other tabs, so the wrong container must not leak in.
        rows = asyncio.run(SteamStoreClient(self._client()).popular_upcoming("CN", 10))
        self.assertNotIn(3058360, [r["appid"] for r in rows])

    def test_the_limit_is_respected(self) -> None:
        rows = asyncio.run(SteamStoreClient(self._client()).popular_upcoming("CN", 1))
        self.assertEqual([r["appid"] for r in rows], [4705510])

    def test_duplicate_rows_keep_their_first_position(self) -> None:
        page = (
            '<div id="tab_upcoming_content">'
            '<a class="tab_row_item" data-ds-appid="5" href="x">'
            '<div class="tab_item_title">First</div></a>'
            '<a class="tab_row_item" data-ds-appid="7" href="x">'
            '<div class="tab_item_title">Second</div></a>'
            '<a class="tab_row_item" data-ds-appid="5" href="x">'
            '<div class="tab_item_title">First again</div></a>'
            "</div>"
        )
        rows = asyncio.run(SteamStoreClient(self._client(page)).popular_upcoming("CN", 10))
        self.assertEqual([r["appid"] for r in rows], [5, 7])

    def test_bundles_are_skipped(self) -> None:
        page = (
            '<div id="tab_upcoming_content">'
            '<a class="tab_row_item" data-ds-appid="100,200" href="x">'
            '<div class="tab_item_title">Bundle</div></a>'
            '<a class="tab_row_item" data-ds-appid="300" href="x">'
            '<div class="tab_item_title">Real Game</div></a>'
            "</div>"
        )
        rows = asyncio.run(SteamStoreClient(self._client(page)).popular_upcoming("CN", 10))
        self.assertEqual([r["appid"] for r in rows], [300])

    def test_a_missing_container_yields_nothing(self) -> None:
        client = self._client("<html></html>")
        self.assertEqual(asyncio.run(SteamStoreClient(client).popular_upcoming("CN")), [])

    def test_the_home_page_fails_over_to_the_second_host(self) -> None:
        # The global storefront drops out intermittently; the China one serves
        # the same shelf from a smaller catalogue.
        page = self.PAGE
        seen: list[str] = []

        class _C:
            async def get(self, url, params=None, **kwargs):
                seen.append(str(url))
                if "store.steamchina.com" not in str(url):
                    raise httpx.ReadTimeout("")
                return _FakeResponse({}, text=page)

        store = SteamStoreClient(_C())
        rows = asyncio.run(store.popular_upcoming("CN", 10))
        self.assertEqual([r["appid"] for r in rows], [4705510, 4358690])
        self.assertTrue(any("store.steamchina.com" in u for u in seen))
        # The caller is expected to say the result came from the smaller site.
        self.assertEqual(store.last_fallback_reason, "https://store.steamchina.com")

    def test_a_healthy_global_page_reports_no_fallback(self) -> None:
        store = SteamStoreClient(self._client())
        asyncio.run(store.popular_upcoming("CN", 10))
        self.assertIsNone(store.last_fallback_reason)

    def test_failures_become_domain_errors(self) -> None:
        with self.assertRaises(SteamApiError):
            asyncio.run(SteamStoreClient(self._client(boom=True)).popular_upcoming("CN"))


class FreeGameListingTests(unittest.TestCase):
    """Free games must survive the rankings without a bogus historical low."""

    ROW = {"appid": 730, "name": "CS2", "final": "", "original": "", "discount": 0, "capsule": ""}

    def test_build_deal_item_keeps_free_rows_when_asked(self) -> None:
        # A discount listing has nothing to show for a free game, but a
        # ranking would be wrong to omit the most played games on Steam.
        self.assertIsNone(build_deal_item(self.ROW, None))
        kept = build_deal_item(self.ROW, None, allow_free=True)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.price.formatted_current, "\u514d\u8d39\u6e38\u73a9")

    def test_paid_rows_are_unaffected(self) -> None:
        row = {**self.ROW, "final": "\u00a533.00"}
        kept = build_deal_item(row, None)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.price.formatted_current, "\u00a533.00")

    def test_service_drops_the_lowest_price_for_free_games(self) -> None:
        # Heybox reports a figure for a paid edition of the same title, which
        # rendered as "史低 96" beside a free-to-play game.
        service = _ranking_service(
            LowestPrice(
                value=Decimal("96"),
                currency="CNY",
                recorded_on="2020-01-01",
                discount_percent=50,
            )
        )
        items = asyncio.run(service._enrich_rows([dict(self.ROW)], "none"))
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].lowest)
        self.assertEqual(items[0].price.formatted_current, "\u514d\u8d39\u6e38\u73a9")

    def test_service_keeps_the_lowest_price_for_paid_games(self) -> None:
        lowest = LowestPrice(
            value=Decimal("20"),
            currency="CNY",
            recorded_on="2023-06-01",
            discount_percent=60,
        )
        service = _ranking_service(lowest)
        row = {**self.ROW, "final": "\u00a533.00"}
        items = asyncio.run(service._enrich_rows([row], "none"))
        self.assertEqual(items[0].lowest, lowest)


class _RankingStore:
    """Minimal store stub for the ranking enrichment path."""

    async def get_items(self, appids, country):  # noqa: ANN001, ANN201
        return {}


def _ranking_service(lowest: LowestPrice) -> SteamDealService:
    """Build a service wired for the ranking enrichment path.

    Args:
        lowest: Value the stubbed Heybox history lookup returns.

    Returns:
        A service whose only collaborator is a store stub and a fixed lowest
        price lookup.
    """
    service = SteamDealService.__new__(SteamDealService)
    service.store = _RankingStore()  # type: ignore[assignment]
    service.country = "CN"

    async def _fake_lowest(appid):  # noqa: ANN001, ANN202
        return lowest

    service._safe_lowest = _fake_lowest  # type: ignore[assignment]
    return service


if __name__ == "__main__":
    unittest.main()

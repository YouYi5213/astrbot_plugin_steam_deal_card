"""Unit tests for the Steam deal card plugin."""

from __future__ import annotations

import asyncio
import io
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def _install_astrbot_stub() -> None:
    """Provide the minimal ``astrbot`` surface the plugin modules import.

    ``service.py`` logs through AstrBot, so importing it outside a running
    install fails. Installing the stub here keeps this file runnable on its own
    instead of relying on another test module having imported first.
    """
    if "astrbot" in sys.modules:
        return

    class _Logger:
        def info(self, *args, **kwargs):
            pass

        warning = info
        error = info
        exception = info

    root = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict
    root.api = api
    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api


_install_astrbot_stub()

import astrbot_plugin_steam_deal_card.render as render_mod  # noqa: E402
from astrbot_plugin_steam_deal_card.models import (  # noqa: E402
    DealItem,
    GameCandidate,
    GameCard,
    LowestPrice,
    PlayerCount,
    PriceInfo,
    ReviewSummary,
    to_decimal,
)
from astrbot_plugin_steam_deal_card.name_match import (  # noqa: E402
    is_confident,
    normalize,
    rank_candidates,
    score_candidate,
    tokenize,
)
from astrbot_plugin_steam_deal_card.render import (  # noqa: E402
    _display_timezone,
    _font,
    _format_end,
    _money,
    _people,
    _truncate,
    _wrap,
    render_candidates,
    render_deals_card,
    render_game_card,
    render_players_card,
)
from astrbot_plugin_steam_deal_card.service import (  # noqa: E402
    LookupError,
    SteamDealService,
    extract_appid,
)
from astrbot_plugin_steam_deal_card.steam_api import (  # noqa: E402
    HeyboxClient,
    SteamApiError,
    SteamSearchClient,
    _format_history_date,
    _is_synthetic_appid,
    _parse_discount_end,
    _parse_specials_page,
    build_deal_item,
    build_game_card,
    capsule_url,
    parse_price,
    parse_reviews,
)

# CJK literals are written as escapes so the file survives any tool that
# rewrites it with a non-UTF-8 default encoding.
TERRARIA_CN = "\u6cf0\u62c9\u745e\u4e9a"  # 泰拉瑞亚
WUKONG_CN = "\u9ed1\u795e\u8bdd\uff1a\u609f\u7a7a"  # 黑神话：悟空
WUKONG_PLAIN = "\u9ed1\u795e\u8bdd\u609f\u7a7a"  # 黑神话悟空
SEKIRO_CN = "\u53ea\u72fc"  # 只狼
SEKIRO_FULL = "\u53ea\u72fc\uff1a\u5f71\u901d\u4e8c\u5ea6"  # 只狼：影逝二度
WITCHER3_CN = "\u5deb\u5e083\uff1a\u72c2\u730e"  # 巫师3：狂猎
WITCHER3_DLC = "\u5deb\u5e083\uff1a\u72c2\u730e - \u8840\u4e0e\u9152"  # 巫师3：狂猎 - 血与酒
ZELDA_CN = "\u585e\u5c14\u8fbe"  # 塞尔达
AVATAR_CN = "\u963f\u51e1\u8fbe\uff1a\u6f58\u591a\u62c9\u8fb9\u5883"  # 阿凡达：潘多拉边境
RDR2_CN = "\u8352\u91ce\u5927\u9556\u5ba22"  # 荒野大镖客2
RDR2_FULL = "\u8352\u91ce\u5927\u9556\u5ba2\uff1a\u6551\u8d4e2"  # 荒野大镖客：救赎2
RDR2_ONLINE = (
    "\u8352\u91ce\u5927\u9556\u5ba2 \u7ebf\u4e0a\u6a21\u5f0f Steam\u7248"  # 荒野大镖客 线上模式
)
FREE_GAME_CN = "\u514d\u8d39\u6e38\u620f"  # 免费游戏
LONG_NAME_CN = "\u8fd9\u662f\u4e00\u4e2a\u6781\u5176\u5197\u957f\u7684\u6e38\u620f\u540d\u79f0"
GAME_CN = "\u6e38\u620f"  # 游戏
GOOD_REVIEWS_CN = "\u597d\u8bc4\u5982\u6f6e"  # 好评如潮
VERY_GOOD_CN = "\u7279\u522b\u597d\u8bc4"  # 特别好评
WITCHER_STEAM = "The Witcher 3"
CANDIDATE_CMD = "steam\u6e38\u620f"  # steam游戏
LONG_WRAP_CN = (
    "\u8fd9\u662f\u4e00\u4e2a\u5f88\u957f\u7684\u4e2d\u6587\u6e38\u620f\u540d\u79f0"
    "\u9700\u8981\u6362\u884c\u663e\u793a"
)  # 这是一个很长的中文游戏名称需要换行显示


class NormalizeTests(unittest.TestCase):
    def test_strips_punctuation_and_case(self) -> None:
        self.assertEqual(normalize("Terraria: Official Soundtrack"), "terrariaofficialsoundtrack")
        self.assertEqual(normalize("  Half-Life 2  "), "halflife2")

    def test_keeps_cjk_characters(self) -> None:
        self.assertEqual(normalize(WUKONG_CN), WUKONG_PLAIN)

    def test_empty_input_is_empty(self) -> None:
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None), "")

    def test_tokenize_splits_on_punctuation(self) -> None:
        self.assertEqual(tokenize("Baldur's Gate 3"), {"baldur", "s", "gate", "3"})


class ScoreCandidateTests(unittest.TestCase):
    def test_exact_match_scores_highest(self) -> None:
        self.assertEqual(score_candidate("Terraria", "Terraria"), 100.0)

    def test_exact_match_ignores_punctuation_differences(self) -> None:
        self.assertEqual(score_candidate(WUKONG_PLAIN, WUKONG_CN), 100.0)

    def test_prefix_beats_contains(self) -> None:
        prefix = score_candidate(SEKIRO_CN, SEKIRO_FULL)
        contains = score_candidate("\u609f\u7a7a", WUKONG_CN)
        self.assertGreater(prefix, contains)
        self.assertEqual(prefix, 85.0)

    def test_unrelated_names_score_zero(self) -> None:
        self.assertEqual(score_candidate(ZELDA_CN, AVATAR_CN), 0.0)

    def test_matches_against_secondary_name(self) -> None:
        # Heybox localizes the title, so an English query only matches Steam's name.
        localized_only = score_candidate("Terraria", TERRARIA_CN)
        with_steam_name = score_candidate("Terraria", TERRARIA_CN, "Terraria")
        self.assertEqual(localized_only, 0.0)
        self.assertEqual(with_steam_name, 100.0)

    def test_empty_query_scores_zero(self) -> None:
        self.assertEqual(score_candidate("", "Terraria"), 0.0)

    def test_a_title_that_inserts_characters_mid_name_still_matches(self) -> None:
        # 荒野大镖客2 is how people ask for 荒野大镖客：救赎2. CJK has no word
        # boundaries, so no prefix or substring test can bridge the inserted
        # 救赎, and the correct appid used to be scored 0 and discarded.
        self.assertEqual(score_candidate(RDR2_CN, RDR2_FULL), 60.0)

    def test_the_subsequence_tier_ranks_below_a_prefix_match(self) -> None:
        gapped = score_candidate(RDR2_CN, RDR2_FULL)
        prefix = score_candidate(SEKIRO_CN, SEKIRO_FULL)
        self.assertLess(gapped, prefix)

    def test_a_gapped_match_needs_enough_coverage(self) -> None:
        # The query must account for most of the title, or every short CJK
        # query would match something long and unrelated.
        self.assertEqual(score_candidate(RDR2_CN, RDR2_ONLINE), 0.0)

    def test_a_short_query_does_not_gap_match(self) -> None:
        # 幻塔 must not match unrelated titles that merely contain 塔.
        self.assertEqual(score_candidate("\u5e7b\u5854", "\u7c73\u5854"), 0.0)
        self.assertEqual(
            score_candidate("\u5e7b\u5854", "Fantasy Grounds - The Tower of Jhedophar"),
            0.0,
        )

    def test_latin_subsequences_do_not_match(self) -> None:
        # "gtav" is a subsequence of "grandtheftautov" but is not a match.
        self.assertEqual(score_candidate("gtav", "Grand Theft Auto V"), 0.0)

    def test_a_shorter_title_inside_a_longer_query_also_matches(self) -> None:
        # The gapped tier works in both directions.
        self.assertEqual(score_candidate(RDR2_FULL, RDR2_CN), 60.0)


class RankCandidatesTests(unittest.TestCase):
    def test_orders_by_score_then_popularity(self) -> None:
        ranked = rank_candidates(
            "Terraria",
            [
                GameCandidate(409210, "Terraria: Official Soundtrack", popularity=5628),
                GameCandidate(105600, TERRARIA_CN, steam_name="Terraria", popularity=758595),
            ],
        )
        self.assertEqual(ranked[0].appid, 105600)
        self.assertEqual(ranked[0].score, 100.0)

    def test_deduplicates_appids(self) -> None:
        ranked = rank_candidates(
            "Terraria",
            [GameCandidate(105600, "Terraria"), GameCandidate(105600, "Terraria")],
        )
        self.assertEqual(len(ranked), 1)

    def test_respects_limit(self) -> None:
        candidates = [GameCandidate(index, f"Terraria {index}") for index in range(1, 20)]
        self.assertEqual(len(rank_candidates("Terraria", candidates, limit=3)), 3)

    def test_keeps_zero_score_candidates_for_the_caller_to_reject(self) -> None:
        ranked = rank_candidates(ZELDA_CN, [GameCandidate(1, AVATAR_CN)])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].score, 0.0)


class IsConfidentTests(unittest.TestCase):
    def test_single_candidate_is_confident(self) -> None:
        self.assertTrue(is_confident([GameCandidate(1, "X", score=10.0)]))

    def test_no_candidates_is_not_confident(self) -> None:
        self.assertFalse(is_confident([]))

    def test_exact_match_wins_over_prefix(self) -> None:
        ranked = [
            GameCandidate(1, "Terraria", score=100.0),
            GameCandidate(2, "Terraria Soundtrack", score=85.0),
        ]
        self.assertTrue(is_confident(ranked))

    def test_two_equal_prefix_matches_are_ambiguous(self) -> None:
        ranked = [
            GameCandidate(1, WITCHER3_CN, score=85.0, popularity=100),
            GameCandidate(2, WITCHER3_CN, score=85.0, popularity=100),
        ]
        self.assertFalse(is_confident(ranked))

    def test_popularity_breaks_a_prefix_tie(self) -> None:
        ranked = [
            GameCandidate(1, TERRARIA_CN, score=85.0, popularity=758595),
            GameCandidate(2, "Terraria Soundtrack", score=85.0, popularity=5628),
        ]
        self.assertTrue(is_confident(ranked))

    def test_weak_scores_are_never_confident(self) -> None:
        ranked = [
            GameCandidate(1, "A", score=50.0, popularity=999999),
            GameCandidate(2, "B", score=40.0),
        ]
        self.assertFalse(is_confident(ranked))


class ExtractAppidTests(unittest.TestCase):
    def test_extracts_from_store_url(self) -> None:
        self.assertEqual(
            extract_appid("https://store.steampowered.com/app/105600/Terraria/"), 105600
        )

    def test_extracts_bare_appid(self) -> None:
        self.assertEqual(extract_appid("105600"), 105600)

    def test_extracts_prefixed_appid(self) -> None:
        self.assertEqual(extract_appid("appid=105600"), 105600)

    def test_returns_none_for_a_game_name(self) -> None:
        self.assertIsNone(extract_appid(TERRARIA_CN))
        self.assertIsNone(extract_appid("Terraria"))

    def test_does_not_treat_a_title_with_numbers_as_an_appid(self) -> None:
        self.assertIsNone(extract_appid("\u5deb\u5e083"))


class ParseSpecialsPageTests(unittest.TestCase):
    HTML = """
    <a href="x" data-ds-appid="2369390" class="search_result_row">
      <div class="search_capsule"><img src="https://img/2369390.jpg" ></div>
      <span class="title">Far Cry&#174; 6</span>
      <div class="discount_block" data-discount="90">
        <div class="discount_original_price">\u00a5298.00</div>
        <div class="discount_final_price">\u00a529.80</div>
      </div>
    </a>
    <a href="y" data-ds-appid="111,222" class="search_result_row">
      <span class="title">Bundle</span>
      <div class="discount_block" data-discount="50">
        <div class="discount_final_price">\u00a510.00</div>
      </div>
    </a>
    """

    def test_parses_a_discounted_row(self) -> None:
        rows = _parse_specials_page(self.HTML)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["appid"], 2369390)
        self.assertEqual(row["name"], "Far Cry\u00ae 6")
        self.assertEqual(row["discount"], 90)
        self.assertEqual(row["final"], "\u00a529.80")
        self.assertEqual(row["original"], "\u00a5298.00")
        self.assertEqual(row["capsule"], "https://img/2369390.jpg")

    def test_skips_bundles_with_multiple_appids(self) -> None:
        appids = [row["appid"] for row in _parse_specials_page(self.HTML)]
        self.assertNotIn(111, appids)

    def test_empty_html_yields_no_rows(self) -> None:
        self.assertEqual(_parse_specials_page(""), [])


class ParsePriceTests(unittest.TestCase):
    def test_parses_a_discounted_price(self) -> None:
        price = parse_price(
            {
                "best_purchase_option": {
                    "formatted_final_price": "\u00a529.80",
                    "formatted_original_price": "\u00a5298.00",
                    "final_price_in_cents": "2980",
                    "original_price_in_cents": "29800",
                    "discount_pct": 90,
                    "active_discounts": [{"discount_end_date": 1790874000}],
                }
            }
        )
        self.assertIsNotNone(price)
        self.assertEqual(price.formatted_current, "\u00a529.80")
        self.assertEqual(price.discount_percent, 90)
        self.assertEqual(price.current_value, Decimal("29.8"))
        self.assertTrue(price.is_discounted)
        self.assertEqual(price.discount_end, datetime.fromtimestamp(1790874000, tz=timezone.utc))

    def test_returns_none_without_a_price(self) -> None:
        self.assertIsNone(parse_price({}))
        self.assertIsNone(parse_price({"best_purchase_option": {}}))

    def test_undiscounted_price_has_no_end_date(self) -> None:
        price = parse_price(
            {
                "best_purchase_option": {
                    "formatted_final_price": "\u00a542.00",
                    "final_price_in_cents": "4200",
                }
            }
        )
        self.assertIsNotNone(price)
        self.assertFalse(price.is_discounted)
        self.assertIsNone(price.discount_end)


class DiscountEndTests(unittest.TestCase):
    def test_picks_the_earliest_end_date(self) -> None:
        end = _parse_discount_end([{"discount_end_date": 200}, {"discount_end_date": 100}])
        self.assertEqual(end, datetime.fromtimestamp(100, tz=timezone.utc))

    def test_handles_missing_and_malformed_entries(self) -> None:
        self.assertIsNone(_parse_discount_end(None))
        self.assertIsNone(_parse_discount_end([]))
        self.assertIsNone(_parse_discount_end([{}, "x", 5]))


class ParseReviewsTests(unittest.TestCase):
    def test_parses_a_review_summary(self) -> None:
        reviews = parse_reviews(
            {
                "reviews": {
                    "summary_filtered": {
                        "review_count": 1240388,
                        "percent_positive": 97,
                        "review_score_label": GOOD_REVIEWS_CN,
                    }
                }
            }
        )
        self.assertIsNotNone(reviews)
        self.assertEqual(reviews.label, GOOD_REVIEWS_CN)
        self.assertEqual(reviews.percent_positive, 97)
        self.assertEqual(reviews.review_count, 1240388)

    def test_returns_none_when_there_are_no_reviews(self) -> None:
        self.assertIsNone(parse_reviews({}))
        self.assertIsNone(parse_reviews({"reviews": {"summary_filtered": {}}}))


class CapsuleUrlTests(unittest.TestCase):
    def test_builds_url_from_the_asset_template(self) -> None:
        url = capsule_url(
            {
                "appid": 105600,
                "assets": {
                    "asset_url_format": "steam/apps/105600/${FILENAME}?t=123",
                    "main_capsule": "capsule_616x353.jpg",
                },
            }
        )
        self.assertEqual(
            url,
            "https://shared.akamai.steamstatic.com/store_item_assets/"
            "steam/apps/105600/capsule_616x353.jpg?t=123",
        )

    def test_falls_back_to_the_header_image(self) -> None:
        url = capsule_url(
            {
                "appid": 1,
                "assets": {"asset_url_format": "steam/apps/1/${FILENAME}", "header": "header.jpg"},
            }
        )
        self.assertTrue(url.endswith("steam/apps/1/header.jpg"))

    def test_builds_a_url_from_the_appid_when_assets_are_absent(self) -> None:
        # The China API omits capsule filenames entirely, so a usable URL is
        # still derived from the appid rather than giving up.
        url = capsule_url({"appid": 1})
        self.assertTrue(url.endswith("steam/apps/1/capsule_616x353.jpg"))

    def test_returns_empty_without_an_appid(self) -> None:
        self.assertEqual(capsule_url({}), "")
        self.assertEqual(capsule_url({"appid": 0}), "")


class BuildCardTests(unittest.TestCase):
    ITEM = {
        "appid": 105600,
        "name": "Terraria",
        "best_purchase_option": {
            "formatted_final_price": "\u00a542.00",
            "final_price_in_cents": "4200",
        },
        "reviews": {
            "summary_filtered": {
                "review_count": 10,
                "percent_positive": 97,
                "review_score_label": GOOD_REVIEWS_CN,
            }
        },
        "assets": {"asset_url_format": "steam/apps/105600/${FILENAME}", "header": "header.jpg"},
        "basic_info": {"developers": [{"name": "Re-Logic"}]},
        "release": {"steam_release_date": 1305568020},
    }

    def test_assembles_a_game_card(self) -> None:
        card = build_game_card(
            105600, self.ITEM, LowestPrice(Decimal("18"), "CNY", "2021-05-14", 50)
        )
        self.assertEqual(card.name, "Terraria")
        self.assertEqual(card.price.formatted_current, "\u00a542.00")
        self.assertEqual(card.reviews.label, GOOD_REVIEWS_CN)
        self.assertEqual(card.developers, ("Re-Logic",))
        self.assertEqual(card.release_date, "2011-05-16")
        self.assertEqual(card.lowest.value, Decimal("18"))
        self.assertEqual(card.store_url, "https://store.steampowered.com/app/105600/")

    def test_falls_back_to_appid_when_the_name_is_missing(self) -> None:
        card = build_game_card(7, {"appid": 7})
        self.assertEqual(card.name, "appid=7")

    def test_builds_a_deal_item_from_a_row(self) -> None:
        row = {
            "appid": 2369390,
            "name": "Far Cry 6",
            "discount": 90,
            "final": "\u00a529.80",
            "original": "\u00a5298.00",
            "capsule": "https://img/x.jpg",
        }
        deal = build_deal_item(row, None)
        self.assertIsNotNone(deal)
        self.assertEqual(deal.name, "Far Cry 6")
        self.assertEqual(deal.price.formatted_current, "\u00a529.80")
        self.assertEqual(deal.price.discount_percent, 90)
        self.assertEqual(deal.capsule_url, "https://img/x.jpg")

    def test_deal_item_prefers_enriched_store_data(self) -> None:
        row = {
            "appid": 1,
            "name": "Row",
            "discount": 50,
            "final": "\u00a51",
            "original": "\u00a52",
            "capsule": "",
        }
        deal = build_deal_item(row, {"appid": 1, "name": "Store Name"})
        self.assertEqual(deal.name, "Store Name")


class HistoryDateTests(unittest.TestCase):
    def test_parses_an_iso_date(self) -> None:
        self.assertEqual(_format_history_date("2021-05-14"), "2021-05-14")

    def test_parses_a_timestamp(self) -> None:
        self.assertEqual(_format_history_date(1620921600), "2021-05-13")

    def test_handles_empty_values(self) -> None:
        self.assertEqual(_format_history_date(None), "")
        self.assertEqual(_format_history_date(""), "")


class ToDecimalTests(unittest.TestCase):
    def test_parses_numeric_strings(self) -> None:
        self.assertEqual(to_decimal("18.5"), Decimal("18.5"))

    def test_rejects_non_numeric_values(self) -> None:
        self.assertIsNone(to_decimal(None))
        self.assertIsNone(to_decimal("abc"))
        self.assertIsNone(to_decimal(True))


class FormatHelpersTests(unittest.TestCase):
    def test_money_uses_currency_symbols(self) -> None:
        self.assertEqual(_money(Decimal("18"), "CNY"), "\u00a518")
        self.assertEqual(_money(Decimal("29.8"), "CNY"), "\u00a529.8")
        self.assertEqual(_money(Decimal("5"), "USD"), "$5")
        self.assertEqual(_money(Decimal("5"), ""), "5")

    def test_format_end_reports_remaining_days(self) -> None:
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(
            _format_end(end, now),
            "\u6298\u6263 2026-10-01 \u7ed3\u675f\uff08\u5269 12 \u5929\uff09",
        )

    def test_format_end_compact_drops_the_year(self) -> None:
        now = datetime(2026, 9, 19, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(
            _format_end(end, now, compact=True),
            "\u6298\u6263 10-01 \u7ed3\u675f\uff08\u5269 12 \u5929\uff09",
        )

    def test_format_end_handles_today_and_past(self) -> None:
        now = datetime(2026, 9, 19, 6, tzinfo=timezone.utc)
        today = datetime(2026, 9, 19, 20, tzinfo=timezone.utc)
        past = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.assertIn("\u4eca\u5929\u7ed3\u675f", _format_end(today, now))
        self.assertIn("\u5df2\u7ed3\u675f", _format_end(past, now))

    def test_format_end_returns_empty_without_a_date(self) -> None:
        self.assertEqual(_format_end(None), "")


class TruncateTests(unittest.TestCase):
    def setUp(self) -> None:
        from PIL import Image, ImageDraw

        from astrbot_plugin_steam_deal_card.render import _font

        self.draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        self.font = _font(24)

    def test_keeps_text_that_fits(self) -> None:
        self.assertEqual(_truncate(self.draw, "abc", self.font, 1000), "abc")

    def test_shortens_text_that_overflows(self) -> None:
        result = _truncate(self.draw, "a" * 200, self.font, 100)
        self.assertTrue(result.endswith("\u2026"))
        self.assertLess(len(result), 200)

    def test_returns_empty_for_zero_width(self) -> None:
        self.assertEqual(_truncate(self.draw, "abc", self.font, 0), "")

    def test_wrap_limits_the_line_count(self) -> None:
        lines = _wrap(self.draw, LONG_WRAP_CN, self.font, 120, max_lines=2)
        self.assertLessEqual(len(lines), 2)


class RenderTests(unittest.TestCase):
    def _game_card(self) -> GameCard:
        return GameCard(
            appid=105600,
            name=f"{TERRARIA_CN} Terraria",
            price=PriceInfo(
                formatted_current="\u00a529.80",
                formatted_original="\u00a542.00",
                discount_percent=29,
                current_value=Decimal("29.8"),
                original_value=Decimal("42"),
                discount_end=datetime(2026, 10, 1, tzinfo=timezone.utc),
            ),
            reviews=ReviewSummary(GOOD_REVIEWS_CN, 97, 1240388),
            capsule_urls=(),
            lowest=LowestPrice(Decimal("18"), "CNY", "2021-05-14", 50),
            release_date="2011-05-16",
            developers=("Re-Logic",),
        )

    def test_renders_a_game_card_png(self) -> None:
        png = render_game_card(self._game_card())
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 5000)

    def test_renders_a_card_without_a_price_or_reviews(self) -> None:
        card = GameCard(
            appid=1, name=FREE_GAME_CN, price=None, reviews=None, capsule_urls=(), is_free=True
        )
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_renders_a_card_with_a_very_long_name(self) -> None:
        card = GameCard(
            appid=1,
            name=LONG_NAME_CN * 6,
            price=None,
            reviews=None,
            capsule_urls=(),
        )
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_renders_a_deals_card(self) -> None:
        deals = [
            DealItem(
                appid=index,
                name=f"{GAME_CN} {index}",
                price=PriceInfo(f"\u00a5{index}.00", "\u00a599.00", 50, discount_end=None),
                capsule_urls=(),
                lowest=LowestPrice(Decimal("5"), "CNY", "2024-01-01", 80),
                reviews=ReviewSummary(VERY_GOOD_CN, 90, 1000),
            )
            for index in range(1, 6)
        ]
        self.assertTrue(render_deals_card(deals).startswith(b"\x89PNG"))

    def test_renders_an_empty_deals_card(self) -> None:
        self.assertTrue(render_deals_card([]).startswith(b"\x89PNG"))

    def test_renders_a_deal_without_lowest_or_reviews(self) -> None:
        deals = [
            DealItem(
                appid=1,
                name=GAME_CN,
                price=PriceInfo("\u00a510.00", "\u00a520.00", 50),
                capsule_urls=(),
            )
        ]
        self.assertTrue(render_deals_card(deals).startswith(b"\x89PNG"))

    def test_renders_the_candidate_list(self) -> None:
        candidates = [
            GameCandidate(292030, WITCHER3_CN, steam_name=WITCHER_STEAM, score=85.0),
            GameCandidate(378648, WITCHER3_DLC, score=85.0),
        ]
        self.assertTrue(
            render_candidates("\u5deb\u5e083", candidates, CANDIDATE_CMD).startswith(b"\x89PNG")
        )

    def test_renders_candidates_with_an_empty_query(self) -> None:
        candidates = [GameCandidate(1, GAME_CN, score=0.0)]
        self.assertTrue(render_candidates("", candidates, CANDIDATE_CMD).startswith(b"\x89PNG"))


class PeopleFormatTests(unittest.TestCase):
    def test_small_counts_are_plain(self) -> None:
        self.assertEqual(_people(0), "0")
        self.assertEqual(_people(999), "999")
        self.assertEqual(_people(9999), "9,999")

    def test_tens_of_thousands_use_wan(self) -> None:
        self.assertEqual(_people(10_000), "1.0 \u4e07")
        self.assertEqual(_people(498_925), "49.9 \u4e07")

    def test_hundreds_of_millions_use_yi(self) -> None:
        self.assertEqual(_people(120_000_000), "1.20 \u4ebf")


class LowestGapTests(unittest.TestCase):
    """The gap to the historical low is the number users actually act on."""

    def _card(self, current, low, currency="CNY"):
        return GameCard(
            appid=1,
            name="G",
            price=PriceInfo(
                formatted_current=f"\u00a5{current}",
                formatted_original=f"\u00a5{current}",
                discount_percent=0,
                currency=currency,
                current_value=Decimal(str(current)),
            ),
            reviews=None,
            lowest=LowestPrice(
                value=Decimal(str(low)),
                currency=currency,
                recorded_on="2024-01-01",
                discount_percent=50,
            ),
        )

    def test_card_renders_with_a_gap_above_the_low(self) -> None:
        png = render_game_card(self._card(58, "19.2"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_card_renders_at_the_low(self) -> None:
        png = render_game_card(self._card(18, "18"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_card_renders_below_the_low(self) -> None:
        # A new historical low on a deeper discount.
        png = render_game_card(self._card(10, "18"))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_card_renders_without_a_current_price(self) -> None:
        card = GameCard(appid=1, name="G", price=None, reviews=None)
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_card_renders_without_a_lowest(self) -> None:
        card = GameCard(
            appid=1,
            name="G",
            price=PriceInfo("¥10", "¥10", 0, current_value=Decimal("10")),
            reviews=None,
            lowest=None,
        )
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_gap_does_not_collide_on_a_narrow_card(self) -> None:
        # The gap badge must not be drawn over the left-hand text; with a huge
        # number it falls back to the parenthesised note instead.
        card = self._card("99999999", "0.01")
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_a_large_gap_changes_the_rendering(self) -> None:
        near = _pixels(render_game_card(self._card(20, "19")))
        far = _pixels(render_game_card(self._card(200, "19")))
        self.assertNotEqual(near, far)


class DescriptionAndLinkTests(unittest.TestCase):
    """The description and store link are already fetched; both must show."""

    def _card(self, description="", name="Game"):
        return GameCard(
            appid=367520,
            name=name,
            price=PriceInfo("¥58.00", "¥58.00", 0, current_value=Decimal("58")),
            reviews=ReviewSummary("好评如潮", 96, 502960),
            short_description=description,
        )

    def test_description_is_drawn(self) -> None:
        without = _pixels(render_game_card(self._card("")))
        with_desc = _pixels(render_game_card(self._card("这是一段游戏简介，用来测试渲染。")))
        self.assertNotEqual(without, with_desc)

    def test_a_long_description_is_capped_not_overflowing(self) -> None:
        short = Image.open(io.BytesIO(render_game_card(self._card("短简介")))).height
        long_card = render_game_card(self._card("很长的一段简介。" * 60))
        self.assertTrue(long_card.startswith(b"\x89PNG"))
        # Capped at three lines, so the card cannot grow without bound.
        self.assertLess(Image.open(io.BytesIO(long_card)).height, short + 140)

    def test_description_can_be_multiline(self) -> None:
        png = render_game_card(self._card("第一句。第二句。第三句。" * 8))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_a_description_with_literal_newlines_renders(self) -> None:
        # 崩坏3's short_description arrives from the store API with embedded
        # newlines, and Pillow refuses to measure multiline text, so the card
        # crashed for it. Whitespace has to be collapsed before measuring.
        for text in (
            "第一行\n第二行",
            "第一行\r\n第二行",
            "第一行\t第二行",
            "  前后都有空白  \n\n",
            "段一\n\n段二\n段三" * 5,
        ):
            with self.subTest(text=text):
                png = render_game_card(self._card(text))
                self.assertTrue(png.startswith(b"\x89PNG"))

    def test_a_multiline_name_does_not_crash_the_card(self) -> None:
        # The name is truncated rather than wrapped, but it gets measured too.
        png = render_game_card(self._card("desc", name="很长的\n游戏\n名字" * 6))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_card_renders_for_every_price_shape(self) -> None:
        for card in (
            self._card("desc"),
            GameCard(1, "A", None, None),
            GameCard(2, "B", PriceInfo("¥1", "¥1", 0), None, is_free=True),
        ):
            self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_store_url_is_not_drawn_on_the_card(self) -> None:
        # The link is delivered as tappable text beside the image, so drawing
        # it on the card too would only add a line nobody can click.
        with_url = render_game_card(GameCard(105600, "Terraria", None, None))
        # Two cards that differ only in appid must render identically, which
        # cannot happen if the appid-derived URL is drawn.
        other = render_game_card(GameCard(999999, "Terraria", None, None))
        self.assertEqual(_pixels(with_url), _pixels(other))


class WrapPunctuationTests(unittest.TestCase):
    """A wrapped line must not end on a dangling sentence mark."""

    def _draw(self):
        return ImageDraw.Draw(Image.new("RGB", (10, 10)))

    def test_no_line_ends_with_a_period(self) -> None:
        text = "挖掘，战斗，探索，建造！在这个动感十足的冒险游戏里没有什么是不可能的。"
        lines = _wrap(self._draw(), text, _font_for_test(21), 300, max_lines=4)
        for line in lines:
            self.assertFalse(line.endswith("。"), line)
            self.assertFalse(line.endswith("，"), line)

    def test_trailing_spaces_are_trimmed(self) -> None:
        lines = _wrap(self._draw(), "aaa bbb ccc ddd eee fff", _font_for_test(20), 80, max_lines=8)
        for line in lines:
            self.assertEqual(line, line.rstrip())

    def test_empty_lines_are_dropped(self) -> None:
        lines = _wrap(self._draw(), "。", _font_for_test(20), 300, max_lines=3)
        self.assertEqual(lines, [])

    def test_wrapping_still_truncates_with_an_ellipsis(self) -> None:
        lines = _wrap(self._draw(), "字" * 200, _font_for_test(21), 200, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))

    def test_latin_words_are_not_split_mid_word(self) -> None:
        # Breaking "aaa bbb" into "aaa bb" / "b ccc" reads as a bug.
        text = "aaa bbb ccc ddd eee fff"
        lines = _wrap(self._draw(), text, _font_for_test(20), 120, max_lines=8)
        for line in lines:
            for word in line.split():
                self.assertIn(word, text.split(), f"{word!r} is not a whole word")

    def test_no_stray_leading_or_trailing_whitespace(self) -> None:
        text = "aaa bbb ccc ddd eee fff"
        for width in (80, 120, 200, 300):
            for line in _wrap(self._draw(), text, _font_for_test(20), width, max_lines=8):
                self.assertEqual(line, line.strip())

    def test_truncation_prefers_a_word_boundary(self) -> None:
        text = "It is a long English description that should wrap at boundaries."
        lines = _wrap(self._draw(), text, _font_for_test(21), 300, max_lines=2)
        # The cut must not leave a half word before the ellipsis.
        self.assertTrue(lines[-1].endswith("…"))

    def test_truncation_does_not_duplicate_text(self) -> None:
        # The tail used to be appended twice, producing "dddfff".
        text = "aaa bbb ccc ddd eee fff"
        lines = _wrap(self._draw(), text, _font_for_test(20), 120, max_lines=2)
        joined = "".join(lines).replace("…", "")
        self.assertLessEqual(len(joined), len(text))

    def test_cjk_truncation_has_no_dangling_punctuation(self) -> None:
        text = "挖掘，战斗，探索，建造！在这个动感十足的冒险游戏里没有什么是不可能的。"
        lines = _wrap(self._draw(), text, _font_for_test(21), 300, max_lines=2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertNotIn("，…", lines[-1])

    def test_a_single_unbreakable_word_is_kept(self) -> None:
        lines = _wrap(
            self._draw(), "Supercalifragilisticexpialidocious", _font_for_test(21), 120, max_lines=2
        )
        self.assertTrue(lines)
        self.assertEqual(lines[0], "Supercalifragilisticexpialidocious")


class DisplayTimezoneTests(unittest.TestCase):
    """The card clock must be Beijing time, and must survive a missing tz db."""

    def test_resolves_to_an_eight_hour_offset(self) -> None:
        tz = _display_timezone()
        offset = datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(tz).utcoffset()
        self.assertEqual(offset, timedelta(hours=8))

    def test_never_raises_without_a_tz_database(self) -> None:
        # Slim images ship no tzdata; the plugin must still render. Forcing the
        # lookup to fail proves the fallback rather than the system database.
        with patch.object(render_mod, "ZoneInfo", side_effect=KeyError("no tzdata")):
            self.assertEqual(_display_timezone().utcoffset(None), timedelta(hours=8))

    def test_utc_midnight_is_eight_in_the_morning(self) -> None:
        midnight = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(midnight.astimezone(_display_timezone()).strftime("%H:%M"), "08:00")

    def test_evening_utc_rolls_into_the_next_day(self) -> None:
        # 20:00 UTC is already the next morning in Beijing.
        evening = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
        shifted = evening.astimezone(_display_timezone())
        self.assertEqual(shifted.strftime("%m-%d %H:%M"), "09-21 04:00")

    def test_naive_datetime_is_accepted(self) -> None:
        # render_players_card takes `now` for tests; a naive value must not
        # crash astimezone.
        png = render_players_card(
            [PlayerCount(1, "X", players=5, rank=1)], now=datetime(2026, 9, 20, 5, 0)
        )
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_fallback_matches_the_real_zone(self) -> None:
        # The fixed offset must equal whatever the tz database reports, or the
        # stamp would differ between deployment images.
        real = _display_timezone()
        moment = datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc)
        with patch.object(render_mod, "ZoneInfo", side_effect=KeyError("no tzdata")):
            fallback = _display_timezone()
        self.assertEqual(moment.astimezone(real), moment.astimezone(fallback))

    def test_card_stamp_uses_beijing_time(self) -> None:
        # 05:00 UTC and 13:00+08:00 are the same instant, so the two cards must
        # be pixel-identical. A UTC stamp would compare the wrong clock.
        entry = [PlayerCount(1, "X", players=5, rank=1)]
        as_utc = render_players_card(entry, now=datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc))
        as_beijing = render_players_card(
            entry, now=datetime(2026, 9, 20, 13, 0, tzinfo=timezone(timedelta(hours=8)))
        )
        self.assertEqual(_pixels(as_utc), _pixels(as_beijing))

    def test_utc_stamp_is_not_used(self) -> None:
        # If the card still stamped UTC, 05:00 UTC would render the same as
        # 05:00+08:00 (which is 21:00 the previous day in UTC). They must differ.
        entry = [PlayerCount(1, "X", players=5, rank=1)]
        utc_morning = render_players_card(
            entry, now=datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc)
        )
        beijing_morning = render_players_card(
            entry, now=datetime(2026, 9, 20, 5, 0, tzinfo=timezone(timedelta(hours=8)))
        )
        self.assertNotEqual(_pixels(utc_morning), _pixels(beijing_morning))

    def test_different_beijing_hours_do_differ(self) -> None:
        # Guards the comparison above: the stamp really is drawn, so an hour
        # change must be visible.
        entry = [PlayerCount(1, "X", players=5, rank=1)]
        one = render_players_card(entry, now=datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc))
        two = render_players_card(entry, now=datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc))
        self.assertNotEqual(_pixels(one), _pixels(two))


def _font_for_test(size: int):
    """Return the renderer's font for a size.

    Args:
        size: Font size in points.

    Returns:
        A Pillow font object.
    """
    return _font(size)


def _pixels(png: bytes) -> bytes:
    """Decode a PNG to raw pixels for exact comparison.

    Args:
        png: Encoded PNG bytes.

    Returns:
        Raw RGB pixel data.
    """
    with Image.open(io.BytesIO(png)) as image:
        return image.convert("RGB").tobytes()


def _png_bytes(size: tuple[int, int], colour: tuple[int, int, int] = (200, 60, 60)) -> bytes:
    """Build a small solid PNG for capsule image tests.

    Args:
        size: ``(width, height)`` of the image.
        colour: RGB fill colour.

    Returns:
        Encoded PNG bytes.
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


class PlayerRenderTests(unittest.TestCase):
    def _entries(self) -> list[PlayerCount]:
        return [
            PlayerCount(
                730, "Counter-Strike 2", players=498887, peak=1317931, peak_date="09-19", rank=1
            ),
            PlayerCount(570, "Dota 2", players=423263, peak=860350, peak_date="09-19", rank=2),
            PlayerCount(1, GAME_CN, players=12, rank=3),
        ]

    def test_renders_a_ranking_png(self) -> None:
        png = render_players_card(self._entries())
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 4000)

    def test_renders_a_single_entry(self) -> None:
        png = render_players_card([PlayerCount(730, "CS2", players=1, rank=1)])
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_renders_without_a_player_count(self) -> None:
        # Steam omits the count for some apps; the card must still render.
        png = render_players_card([PlayerCount(1, GAME_CN, players=None, rank=1)])
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_renders_without_a_peak(self) -> None:
        png = render_players_card([PlayerCount(1, GAME_CN, players=5, peak=None, rank=1)])
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_renders_a_long_name(self) -> None:
        entry = PlayerCount(1, LONG_NAME_CN * 6, players=5, rank=1)
        self.assertTrue(render_players_card([entry]).startswith(b"\x89PNG"))

    def test_taller_cards_hold_more_rows(self) -> None:
        short = render_players_card([PlayerCount(1, GAME_CN, players=1, rank=1)])
        tall = render_players_card(self._entries())

        def height(png: bytes) -> int:
            return Image.open(io.BytesIO(png)).height

        self.assertGreater(height(tall), height(short))

    def test_renders_with_cover_images(self) -> None:
        card = render_players_card(
            self._entries(), {730: _png_bytes((240, 135)), 570: _png_bytes((240, 135))}
        )
        self.assertTrue(card.startswith(b"\x89PNG"))

    def test_a_broken_cover_falls_back_to_the_placeholder(self) -> None:
        # Garbage bytes must not take down the card.
        card = render_players_card(self._entries(), {730: b"not an image"})
        self.assertTrue(card.startswith(b"\x89PNG"))

    def test_covers_change_the_output(self) -> None:
        # Proves the capsule bytes actually reach the canvas.
        plain = render_players_card(self._entries())
        with_cover = render_players_card(self._entries(), {730: _png_bytes((240, 135))})
        self.assertNotEqual(plain, with_cover)

    def test_empty_entries_still_render(self) -> None:
        self.assertTrue(render_players_card([]).startswith(b"\x89PNG"))

    def test_title_override_is_used(self) -> None:
        with_title = render_players_card(self._entries(), title="CUSTOM")
        default = render_players_card(self._entries())
        self.assertNotEqual(with_title, default)


class _StubStore:
    """Store stub returning canned chart and player counts."""

    def __init__(self, chart: list[dict], counts: dict[int, int | None], items: dict) -> None:
        self._chart = chart
        self._counts = counts
        self._items = items
        self.counted: list[int] = []

    async def most_played(self, limit: int = 100) -> list[dict]:
        return self._chart[:limit]

    async def current_players(self, appid: int) -> int | None:
        self.counted.append(appid)
        return self._counts.get(appid)

    async def get_items(self, appids, country):  # noqa: ANN001, ANN201
        return {appid: self._items[appid] for appid in appids if appid in self._items}


class _StubHeybox:
    async def search(self, query):  # noqa: ANN001, ANN201
        return []

    async def lowest_price(self, appid, country="cn"):  # noqa: ANN001, ANN201
        return None


def _service(store: _StubStore) -> SteamDealService:
    return SteamDealService(
        store=store,  # type: ignore[arg-type]
        heybox=_StubHeybox(),  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        country="CN",
        history_country="cn",
        max_players=20,
    )


class TopPlayersTests(unittest.TestCase):
    """The chart is peak-ordered, so the result must be re-sorted by live count."""

    def test_sorts_descending_by_live_count(self) -> None:
        # Chart order is deliberately the reverse of the live order.
        chart = [
            {"appid": 1, "peak_in_game": 900},
            {"appid": 2, "peak_in_game": 800},
            {"appid": 3, "peak_in_game": 700},
        ]
        counts = {1: 10, 2: 5000, 3: 300}
        items = {1: {"name": "A"}, 2: {"name": "B"}, 3: {"name": "C"}}
        entries = asyncio.run(_service(_StubStore(chart, counts, items)).top_players(3))
        self.assertEqual([e.appid for e in entries], [2, 3, 1])
        self.assertEqual([e.players for e in entries], [5000, 300, 10])

    def test_assigns_ranks_after_sorting(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 9}, {"appid": 2, "peak_in_game": 8}]
        counts = {1: 1, 2: 2}
        items = {1: {"name": "A"}, 2: {"name": "B"}}
        entries = asyncio.run(_service(_StubStore(chart, counts, items)).top_players(2))
        self.assertEqual([e.rank for e in entries], [1, 2])
        self.assertEqual(entries[0].appid, 2)

    def test_keeps_the_chart_peak_for_context(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 999}]
        entries = asyncio.run(
            _service(_StubStore(chart, {1: 5}, {1: {"name": "A"}})).top_players(1)
        )
        self.assertEqual(entries[0].peak, 999)
        self.assertEqual(entries[0].players, 5)

    def test_the_peak_carries_the_day_it_covers(self) -> None:
        # The chart figure is a completed day's peak, not today's, so the entry
        # has to say which day it belongs to.
        # 1789776000 = 2026-09-19 00:00:00 UTC
        chart = [{"appid": 1, "peak_in_game": 999, "rollup_date": 1789776000}]
        entries = asyncio.run(
            _service(_StubStore(chart, {1: 5}, {1: {"name": "A"}})).top_players(1)
        )
        self.assertEqual(entries[0].peak_date, "09-19")

    def test_a_missing_rollup_date_leaves_the_label_bare(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 999}]
        entries = asyncio.run(
            _service(_StubStore(chart, {1: 5}, {1: {"name": "A"}})).top_players(1)
        )
        self.assertEqual(entries[0].peak_date, "")

    def test_a_single_lookup_also_carries_the_peak_date(self) -> None:
        chart = [{"appid": 730, "peak_in_game": 999, "rollup_date": 1789776000}]
        entry = asyncio.run(
            _service(_StubStore(chart, {730: 5}, {730: {"name": "CS2"}})).player_count(730)
        )
        self.assertEqual(entry.peak, 999)
        self.assertEqual(entry.peak_date, "09-19")

    def test_entries_carry_cover_urls(self) -> None:
        # The cover is what makes the card readable, so it must survive the
        # store-item lookup rather than being dropped.
        chart = [{"appid": 1, "peak_in_game": 9}]
        items = {
            1: {
                "name": "A",
                "assets": {
                    "asset_url_format": "steam/apps/1/${FILENAME}",
                    "main_capsule": "capsule_616x353.jpg",
                },
            }
        }
        entries = asyncio.run(_service(_StubStore(chart, {1: 5}, items)).top_players(1))
        self.assertTrue(entries[0].capsule_urls)
        self.assertIn("capsule_616x353.jpg", entries[0].capsule_url)

    def test_entries_without_store_items_have_no_covers(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 9}]
        entries = asyncio.run(_service(_StubStore(chart, {1: 5}, {})).top_players(1))
        self.assertEqual(entries[0].capsule_urls, ())
        self.assertEqual(entries[0].capsule_url, "")

    def test_limit_truncates_after_sorting(self) -> None:
        chart = [{"appid": i, "peak_in_game": 100 - i} for i in range(1, 9)]
        counts = {i: i * 10 for i in range(1, 9)}
        items = {i: {"name": f"G{i}"} for i in range(1, 9)}
        entries = asyncio.run(_service(_StubStore(chart, counts, items)).top_players(3))
        self.assertEqual([e.appid for e in entries], [8, 7, 6])
        self.assertEqual([e.rank for e in entries], [1, 2, 3])

    def test_apps_without_a_count_are_dropped(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 5}, {"appid": 2, "peak_in_game": 4}]
        entries = asyncio.run(
            _service(
                _StubStore(chart, {1: None, 2: 7}, {1: {"name": "A"}, 2: {"name": "B"}})
            ).top_players(5)
        )
        self.assertEqual([e.appid for e in entries], [2])

    def test_missing_name_falls_back_to_the_appid(self) -> None:
        chart = [{"appid": 4242, "peak_in_game": 5}]
        entries = asyncio.run(_service(_StubStore(chart, {4242: 5}, {})).top_players(1))
        self.assertIn("4242", entries[0].name)

    def test_empty_chart_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            asyncio.run(_service(_StubStore([], {}, {})).top_players(5))

    def test_all_counts_missing_raises_lookup_error(self) -> None:
        chart = [{"appid": 1, "peak_in_game": 5}]
        with self.assertRaises(LookupError):
            asyncio.run(_service(_StubStore(chart, {1: None}, {1: {"name": "A"}})).top_players(5))

    def test_does_not_query_the_whole_chart_for_a_small_limit(self) -> None:
        chart = [{"appid": i, "peak_in_game": 1000 - i} for i in range(1, 60)]
        counts = {i: i for i in range(1, 60)}
        items = {i: {"name": f"G{i}"} for i in range(1, 60)}
        store = _StubStore(chart, counts, items)
        asyncio.run(_service(store).top_players(3))
        self.assertLess(len(store.counted), len(chart))
        self.assertGreaterEqual(len(store.counted), 3)

    def test_concurrent_count_requests_are_bounded(self) -> None:
        chart = [{"appid": i, "peak_in_game": 1} for i in range(1, 41)]
        counts = {i: i for i in range(1, 41)}
        items = {i: {"name": f"G{i}"} for i in range(1, 41)}
        peak = {"now": 0}

        class _CountingStore(_StubStore):
            async def current_players(self, appid: int) -> int | None:
                peak["now"] += 1
                peak["max"] = max(peak.get("max", 0), peak["now"])
                try:
                    return await super().current_players(appid)
                finally:
                    peak["now"] -= 1

        asyncio.run(_service(_CountingStore(chart, counts, items)).top_players(30))
        self.assertLessEqual(peak["max"], 12)


class PlayerCountTests(unittest.TestCase):
    def test_single_player_count_uses_the_store_name(self) -> None:
        store = _StubStore([], {730: 498887}, {730: {"name": "Counter-Strike 2"}})
        entry = asyncio.run(_service(store).player_count(730))
        self.assertEqual(entry.name, "Counter-Strike 2")
        self.assertEqual(entry.players, 498887)
        self.assertEqual(entry.rank, 1)

    def test_single_player_count_without_store_data(self) -> None:
        # The count alone is still useful when the store item is unavailable.
        store = _StubStore([], {730: 123}, {})
        entry = asyncio.run(_service(store).player_count(730))
        self.assertEqual(entry.players, 123)
        self.assertIn("730", entry.name)

    def test_unknown_appid_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            asyncio.run(_service(_StubStore([], {}, {})).player_count(999999))

    def test_single_player_count_carries_cover_urls(self) -> None:
        items = {
            730: {
                "name": "Counter-Strike 2",
                "assets": {
                    "asset_url_format": "steam/apps/730/${FILENAME}",
                    "main_capsule": "capsule_616x353.jpg",
                },
            }
        }
        entry = asyncio.run(_service(_StubStore([], {730: 1}, items)).player_count(730))
        self.assertTrue(entry.capsule_urls)


class SyntheticAppidTests(unittest.TestCase):
    """Heybox hands out its own ids for games it has no Steam id for.

    Those ids never exist on Steam, so trusting them makes a resolvable game
    look missing. The motivating case is 崩坏3, which is 1668940 on Steam but
    900045980 on Heybox.
    """

    class _Client:
        def __init__(self, games):
            self._games = games

        async def get(self, url, params=None, **kwargs):  # noqa: ANN001, ANN201
            payload = {"result": {"games": self._games}}

            class _R:
                status_code = 200
                text = ""

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return payload

            return _R()

    def _search(self, games):
        return asyncio.run(HeyboxClient(self._Client(games)).search("x"))

    def test_synthetic_ids_are_recognised(self) -> None:
        # PC/console placeholders are 9 digits; real Steam ids are ~4 million.
        self.assertTrue(_is_synthetic_appid(900045980))
        self.assertTrue(_is_synthetic_appid(900017301))
        self.assertFalse(_is_synthetic_appid(1668940))
        self.assertFalse(_is_synthetic_appid(105600))

    def test_a_synthetic_pc_row_is_dropped(self) -> None:
        # Exactly the 崩坏3 shape: PC game row whose only id is synthetic.
        rows = [
            {
                "name": "Honkai Impact 3rd",
                "steam_appid": 900045980,
                "game_type": "pc",
                "type": "game",
                "follow_num": 185,
            }
        ]
        self.assertEqual(self._search(rows), [])

    def test_a_real_pc_row_survives(self) -> None:
        rows = [
            {
                "name": "泰拉瑞亚",
                "steam_appid": 105600,
                "game_type": "pc",
                "type": "game",
                "follow_num": 758848,
            }
        ]
        found = self._search(rows)
        self.assertEqual([c.appid for c in found], [105600])

    def test_a_real_row_is_kept_alongside_a_synthetic_one(self) -> None:
        rows = [
            {"name": "A", "steam_appid": 900017301, "game_type": "pc", "type": "game"},
            {"name": "B", "steam_appid": 292030, "game_type": "pc", "type": "game"},
        ]
        self.assertEqual([c.appid for c in self._search(rows)], [292030])

    def test_non_pc_and_non_game_rows_are_still_dropped(self) -> None:
        rows = [
            {"name": "Mobile", "steam_appid": 99935083, "game_type": "mobile"},
            {"name": "Console", "steam_appid": 105600, "game_type": "console"},
            {"name": "OST", "steam_appid": 105600, "game_type": "pc", "type": "ost"},
            {"name": "Game", "steam_appid": 105600, "game_type": "pc", "type": "game"},
        ]
        self.assertEqual([c.appid for c in self._search(rows)], [105600])

    def test_junk_rows_do_not_crash(self) -> None:
        rows = ["junk", None, 5, {"name": "no id", "game_type": "pc"}]
        self.assertEqual(self._search(rows), [])


class SteamSearchFallbackTests(unittest.TestCase):
    """The JSON search endpoint only exists on the global host.

    When that host is unreachable the search results endpoint still answers from
    the China host, which is what makes 崩坏3 resolvable on a mainland server.
    """

    class _Client:
        """Serves the two endpoints, and fails the global host on demand."""

        RESULTS_HTML = (
            '<a href="https://store.steampowered.com/app/1668940/3/" '
            'class="search_result_row ds_collapse_flag" data-ds-appid="1668940">'
            '<span class="title">崩坏3</span></a>'
        )

        def __init__(
            self, json_ok=True, global_results_ok=True, json_items=None, results_html=None
        ):
            self.json_ok = json_ok
            self.global_results_ok = global_results_ok
            self.json_items = (
                [{"id": 1668940, "name": "崩坏3"}] if json_items is None else json_items
            )
            self.results_html = self.RESULTS_HTML if results_html is None else results_html
            self.urls = []

        async def get(self, url, params=None, **kwargs):  # noqa: ANN001, ANN201
            target = str(url)
            self.urls.append(target)
            # The China host always answers the results endpoint; that is the
            # whole point of the fallback.
            china = "store.steamchina.com" in target
            if "api/storesearch" in target:
                if not self.json_ok:
                    raise httpx.ConnectTimeout("")
                payload = {"total": len(self.json_items), "items": self.json_items}
            else:
                if not china and not self.global_results_ok:
                    raise httpx.ConnectTimeout("")
                payload = {"success": 1, "results_html": self.results_html}

            class _R:
                status_code = 200
                text = ""

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return payload

            return _R()

    def test_the_json_endpoint_is_used_first(self) -> None:
        client = self._Client()
        found = asyncio.run(SteamSearchClient(client).search("崩坏3"))
        self.assertEqual([c.appid for c in found], [1668940])
        self.assertTrue(any("api/storesearch" in u for u in client.urls))

    def test_it_falls_back_to_the_results_endpoint(self) -> None:
        # The global storefront is down entirely, which is the real condition
        # on the mainland server; the China host still resolves the game.
        client = self._Client(json_ok=False, global_results_ok=False)
        found = asyncio.run(SteamSearchClient(client).search("崩坏3"))
        self.assertEqual([c.appid for c in found], [1668940])
        self.assertTrue(any("/search/results/" in u for u in client.urls))
        self.assertTrue(any("store.steamchina.com" in u for u in client.urls))

    def test_an_empty_json_answer_is_not_taken_as_final(self) -> None:
        # The JSON index is narrower than the results one, so an empty answer
        # from it must not stop the search.
        client = self._Client(json_items=[])
        found = asyncio.run(SteamSearchClient(client).search("只狼"))
        self.assertEqual([c.appid for c in found], [1668940])
        self.assertTrue(any("/search/results/" in u for u in client.urls))

    def test_no_results_anywhere_is_an_empty_answer_not_an_error(self) -> None:
        client = self._Client(json_items=[], results_html="")
        found = asyncio.run(SteamSearchClient(client).search("不存在"))
        self.assertEqual(found, [])

    def test_both_failing_raises(self) -> None:
        class _Dead:
            async def get(self, url, params=None, **kwargs):  # noqa: ANN001, ANN201
                raise httpx.ConnectTimeout("")

        with self.assertRaises(SteamApiError):
            asyncio.run(SteamSearchClient(_Dead()).search("崩坏3"))


if __name__ == "__main__":
    unittest.main()

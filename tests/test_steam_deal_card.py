"""Unit tests for the Steam deal card plugin."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_steam_deal_card.models import (  # noqa: E402
    DealItem,
    GameCandidate,
    GameCard,
    LowestPrice,
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
    _format_end,
    _money,
    _truncate,
    _wrap,
    render_candidates,
    render_deals_card,
    render_game_card,
)
from astrbot_plugin_steam_deal_card.service import extract_appid  # noqa: E402
from astrbot_plugin_steam_deal_card.steam_api import (  # noqa: E402
    _format_history_date,
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

    def test_returns_empty_without_assets(self) -> None:
        self.assertEqual(capsule_url({"appid": 1}), "")


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
            capsule_url="",
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
            appid=1, name=FREE_GAME_CN, price=None, reviews=None, capsule_url="", is_free=True
        )
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_renders_a_card_with_a_very_long_name(self) -> None:
        card = GameCard(
            appid=1,
            name=LONG_NAME_CN * 6,
            price=None,
            reviews=None,
            capsule_url="",
        )
        self.assertTrue(render_game_card(card).startswith(b"\x89PNG"))

    def test_renders_a_deals_card(self) -> None:
        deals = [
            DealItem(
                appid=index,
                name=f"{GAME_CN} {index}",
                price=PriceInfo(f"\u00a5{index}.00", "\u00a599.00", 50, discount_end=None),
                capsule_url="",
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
                capsule_url="",
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


if __name__ == "__main__":
    unittest.main()

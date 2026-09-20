"""Regression tests for the AstrBot command binding contract.

These guard constraints that are invisible at runtime until a user types a
command, at which point the message would be silently ignored.

Background: AstrBot's ``CommandFilter`` only matches when
``event.is_at_or_wake_command`` is true, which requires the message to start
with the configured ``wake_prefix`` (or @-mention the bot, or arrive in a
private chat). On a deployment whose wake prefix is a bot nickname, a bare
``steam\u6e38\u620f \u6cf0\u62c9\u745e\u4e9a`` never reaches a command handler and produces no
log at all. ``RegexFilter`` is explicitly exempt from that gate, so both
commands are registered as regex filters.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
_MODULE = ast.parse(MAIN_SOURCE)

# CJK written as escapes so the file survives any tool that rewrites it with a
# non-UTF-8 default encoding.
GAME = "steam\u6e38\u620f"  # steam游戏
GAME_QUERY = "steam\u6e38\u620f\u67e5\u8be2"  # steam游戏查询
PRICE = "steam\u67e5\u4ef7"  # steam查价
PRICE2 = "steam\u4ef7\u683c"  # steam价格
DEALS = "steam\u6253\u6298"  # steam打折
DEALS2 = "steam\u7279\u60e0"  # steam特惠
PLAYERS = "steam\u5728\u7ebf"  # steam在线
PLAYERS_FULL = "steam\u5728\u7ebf\u4eba\u6570"  # steam在线人数
HOT = "steam\u70ed\u5ea6"  # steam热度
HOT_FULL = "steam\u70ed\u5ea6\u699c"  # steam热度榜
RANK = "steam\u6392\u884c"  # steam排行
PLAYERS_BOARD = "steam\u5728\u7ebf\u699c"  # steam在线榜
TERRARIA = "\u6cf0\u62c9\u745e\u4e9a"  # 泰拉瑞亚
ASKING = "\u8bf7\u95ee"  # 请问


def _install_astrbot_stub() -> None:
    """Install a minimal astrbot stub so main.py can be imported.

    main.py imports framework symbols that only exist inside a running AstrBot
    install. Stubbing them lets the tests exercise the real module (and its real
    regex construction) in a bare checkout. The decorators are pass-through, so
    the registration contract is asserted from the AST instead.
    """

    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    class _Filter:
        def regex(self, *args, **kwargs):
            def decorate(func):
                return func

            return decorate

        def command(self, *args, **kwargs):
            def decorate(func):
                return func

            return decorate

    class _Star:
        def __init__(self, context=None):
            pass

    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = type("Context", (), {})
    star_mod.Star = _Star

    def _register(*args, **kwargs):
        def decorate(cls):
            return cls

        return decorate

    star_mod.register = _register

    class _Image:
        """Minimal stand-in for the real image component.

        Only the ``file`` keyword matters here: the real component treats a
        ``base64://`` value in that slot as inline data rather than a path.
        """

        def __init__(self, **kwargs):
            self.file = kwargs.get("file", "")

    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Image = _Image
    api.message_components = components_mod

    root = types.ModuleType("astrbot")
    root.api = api

    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = components_mod


_install_astrbot_stub()

import astrbot_plugin_steam_deal_card.main as plugin_main  # noqa: E402


def _handler(name: str) -> ast.AsyncFunctionDef:
    """Find an async handler in main.py's AST.

    Args:
        name: Function name to find.

    Returns:
        The matching function definition.
    """
    return next(
        node
        for node in ast.walk(_MODULE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _filter_decorators(handler: ast.AsyncFunctionDef) -> list[str]:
    """Collect the filter attribute names used on a handler.

    Args:
        handler: The handler definition.

    Returns:
        Attribute names such as ``regex`` or ``command``.
    """
    found = []
    for decorator in handler.decorator_list:
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
            found.append(decorator.func.attr)
    return found


class WakePrefixContractTests(unittest.TestCase):
    def test_game_handler_uses_regex_not_command(self) -> None:
        # CommandFilter is gated on wake_prefix; RegexFilter is not.
        decorators = _filter_decorators(_handler("steam_game_command"))
        self.assertIn("regex", decorators)
        self.assertNotIn("command", decorators)

    def test_deals_handler_uses_regex_not_command(self) -> None:
        decorators = _filter_decorators(_handler("steam_deals_command"))
        self.assertIn("regex", decorators)
        self.assertNotIn("command", decorators)

    def test_handlers_do_not_depend_on_greedy_str(self) -> None:
        # GreedyStr only exists for command filters; a regex handler parses the
        # raw message itself.
        imported = any(
            isinstance(node, ast.ImportFrom) and "GreedyStr" in {a.name for a in node.names}
            for node in ast.walk(_MODULE)
        )
        self.assertFalse(imported)
        for name in ("steam_game_command", "steam_deals_command"):
            args = [a.arg for a in _handler(name).args.args]
            self.assertEqual(args, ["self", "event"])

    def test_main_does_not_postpone_annotations(self) -> None:
        self.assertFalse(
            any(
                isinstance(node, ast.ImportFrom) and node.module == "__future__"
                for node in _MODULE.body
            ),
            "main.py must not use `from __future__ import annotations`",
        )


class ImageDeliveryTests(unittest.TestCase):
    """Images must ride the ``file`` field, not the resolver path."""

    def test_no_handler_uses_image_result(self) -> None:
        # event.image_result() resolves its argument as a media URL; handing it
        # a base64 payload produced "/AstrBot/base64:/iVBOR..." and
        # "[Errno 36] File name too long" when the chain was sent.
        offenders = [
            ast.unparse(node)
            for node in ast.walk(_MODULE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "image_result"
        ]
        self.assertEqual(offenders, [])

    def test_handlers_delegate_to_the_shared_helper(self) -> None:
        calls = [
            node
            for node in ast.walk(_MODULE)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_image_result"
        ]
        # deals, game card, candidate list, single player count, ranking.
        self.assertEqual(len(calls), 5)

    def test_every_image_sending_handler_uses_the_helper(self) -> None:
        senders = [
            node.name
            for node in ast.walk(_MODULE)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_image_result"
                for inner in ast.walk(node)
            )
        ]
        self.assertEqual(
            sorted(senders),
            sorted(
                [
                    "steam_deals_command",
                    "steam_players_command",
                    "steam_hot_command",
                    "_render_card",
                    "_render_candidates",
                ]
            ),
        )

    def test_helper_builds_a_base64_image_component(self) -> None:
        helper = next(
            node
            for node in ast.walk(_MODULE)
            if isinstance(node, ast.FunctionDef) and node.name == "_image_result"
        )
        source = ast.unparse(helper)
        self.assertIn("chain_result", source)
        self.assertIn("Comp.Image", source)
        self.assertIn("base64://", source)

    def test_helper_returns_a_real_component(self) -> None:
        class _Event:
            def chain_result(self, chain):
                return chain

        image = plugin_main._image_result(_Event(), b"\x89PNG\x00")
        self.assertEqual(len(image), 1)
        self.assertTrue(image[0].file.startswith("base64://"))
        self.assertEqual(base64.b64decode(image[0].file[len("base64://") :]), b"\x89PNG\x00")

    def test_message_components_are_imported(self) -> None:
        imported = {
            alias.name
            for node in ast.walk(_MODULE)
            if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api"
            for alias in node.names
        }
        self.assertIn("message_components", imported)


class CommandRegexTests(unittest.TestCase):
    def test_game_regex_matches_bare_and_slashed_forms(self) -> None:
        for text in (f"{GAME} {TERRARIA}", f"/{GAME} {TERRARIA}", GAME):
            self.assertRegex(text, plugin_main._GAME_CMD_RE)

    def test_game_regex_accepts_every_alias(self) -> None:
        for name in plugin_main._GAME_COMMANDS:
            self.assertRegex(f"{name} {TERRARIA}", plugin_main._GAME_CMD_RE)
            self.assertRegex(f"/{name} {TERRARIA}", plugin_main._GAME_CMD_RE)

    def test_deals_regex_accepts_every_alias(self) -> None:
        for name in plugin_main._DEALS_COMMANDS:
            self.assertRegex(name, plugin_main._DEALS_CMD_RE)
            self.assertRegex(f"/{name} 15", plugin_main._DEALS_CMD_RE)

    def test_game_regex_does_not_match_the_deals_command(self) -> None:
        self.assertNotRegex(DEALS, plugin_main._GAME_CMD_RE)
        self.assertNotRegex(DEALS2, plugin_main._GAME_CMD_RE)

    def test_deals_regex_does_not_match_the_game_command(self) -> None:
        self.assertNotRegex(f"{GAME} {TERRARIA}", plugin_main._DEALS_CMD_RE)

    def test_regexes_do_not_match_unrelated_text(self) -> None:
        for text in (TERRARIA, "steam", f"\u4eca\u5929{GAME}", f"\u7fa4\u53cb\u8bf4{DEALS}\u4e86"):
            self.assertNotRegex(text, plugin_main._GAME_CMD_RE)
            self.assertNotRegex(text, plugin_main._DEALS_CMD_RE)

    def test_command_names_are_anchored_at_the_start(self) -> None:
        # A message merely containing the command must not trigger it.
        self.assertIsNone(plugin_main._GAME_CMD_RE.match(f"{ASKING} {GAME} {TERRARIA}"))

    def test_regex_allows_trailing_argument_or_end_of_string(self) -> None:
        self.assertIsNotNone(plugin_main._GAME_CMD_RE.match(GAME))
        self.assertIsNotNone(plugin_main._GAME_CMD_RE.match(f"{GAME} x"))
        # A command glued to another word must not match.
        self.assertIsNone(plugin_main._GAME_CMD_RE.match(f"{GAME}x"))


class PlayerCommandRegexTests(unittest.TestCase):
    """The two player count commands must behave like the existing ones."""

    def test_player_regex_accepts_every_alias(self) -> None:
        for name in plugin_main._PLAYERS_COMMANDS:
            self.assertRegex(name, plugin_main._PLAYERS_CMD_RE)
            self.assertRegex(f"/{name} {TERRARIA}", plugin_main._PLAYERS_CMD_RE)

    def test_hot_regex_accepts_every_alias(self) -> None:
        for name in plugin_main._HOT_COMMANDS:
            self.assertRegex(name, plugin_main._HOT_CMD_RE)
            self.assertRegex(f"/{name} 10", plugin_main._HOT_CMD_RE)

    def test_player_regex_does_not_match_the_hot_command(self) -> None:
        self.assertNotRegex(HOT, plugin_main._PLAYERS_CMD_RE)
        self.assertNotRegex(HOT_FULL, plugin_main._PLAYERS_CMD_RE)

    def test_hot_regex_does_not_match_the_player_command(self) -> None:
        self.assertNotRegex(PLAYERS, plugin_main._HOT_CMD_RE)
        self.assertNotRegex(PLAYERS_FULL, plugin_main._HOT_CMD_RE)

    def test_player_regex_does_not_match_the_lookup_commands(self) -> None:
        self.assertNotRegex(f"{GAME} {TERRARIA}", plugin_main._PLAYERS_CMD_RE)
        self.assertNotRegex(DEALS, plugin_main._PLAYERS_CMD_RE)
        self.assertNotRegex(f"{GAME} {TERRARIA}", plugin_main._HOT_CMD_RE)
        self.assertNotRegex(DEALS, plugin_main._HOT_CMD_RE)

    def test_lookup_regexes_do_not_match_the_player_commands(self) -> None:
        self.assertNotRegex(PLAYERS, plugin_main._GAME_CMD_RE)
        self.assertNotRegex(HOT, plugin_main._GAME_CMD_RE)
        self.assertNotRegex(PLAYERS, plugin_main._DEALS_CMD_RE)
        self.assertNotRegex(HOT, plugin_main._DEALS_CMD_RE)

    def test_player_regex_is_anchored_and_bounded(self) -> None:
        self.assertIsNotNone(plugin_main._PLAYERS_CMD_RE.match(PLAYERS))
        self.assertIsNotNone(plugin_main._PLAYERS_CMD_RE.match(f"{PLAYERS} {TERRARIA}"))
        self.assertIsNone(plugin_main._PLAYERS_CMD_RE.match(f"{PLAYERS}\u4eba"))
        self.assertIsNone(plugin_main._PLAYERS_CMD_RE.match(f"{ASKING} {PLAYERS}"))

    def test_hot_regex_is_anchored_and_bounded(self) -> None:
        self.assertIsNotNone(plugin_main._HOT_CMD_RE.match(HOT))
        self.assertIsNotNone(plugin_main._HOT_CMD_RE.match(f"{HOT} 5"))
        # HOT_FULL is its own alias, so it matches; a bare suffix is not a
        # command at all.
        self.assertIsNotNone(plugin_main._HOT_CMD_RE.match(HOT_FULL))
        self.assertIsNone(plugin_main._HOT_CMD_RE.match(f"{HOT}\u699c\u5355"))
        self.assertIsNone(plugin_main._HOT_CMD_RE.match(f"{ASKING} {HOT}"))

    def test_unrelated_text_matches_neither(self) -> None:
        for text in (TERRARIA, "steam", f"\u4eca\u5929{PLAYERS}", f"\u7fa4\u53cb\u8bf4{HOT}\u4e86"):
            self.assertNotRegex(text, plugin_main._PLAYERS_CMD_RE)
            self.assertNotRegex(text, plugin_main._HOT_CMD_RE)

    def test_both_commands_are_registered_as_regex_handlers(self) -> None:
        # Same wake-prefix exemption the other two commands rely on.
        for handler in ("steam_players_command", "steam_hot_command"):
            decorators = _filter_decorators(_handler(handler))
            self.assertIn("regex", decorators)
            self.assertNotIn("command", decorators)

    def test_player_handlers_take_only_self_and_event(self) -> None:
        for handler in ("steam_players_command", "steam_hot_command"):
            args = [a.arg for a in _handler(handler).args.args]
            self.assertEqual(args, ["self", "event"])

    def test_no_command_name_is_a_prefix_of_another_without_a_separator(self) -> None:
        names = (
            list(plugin_main._GAME_COMMANDS)
            + list(plugin_main._DEALS_COMMANDS)
            + list(plugin_main._PLAYERS_COMMANDS)
            + list(plugin_main._HOT_COMMANDS)
        )
        # Longest first everywhere, so a shorter alias cannot shadow a longer
        # one that starts with it.
        for shorter, longer in zip(names, names[1:], strict=False):
            if longer.startswith(shorter):
                self.assertGreaterEqual(len(shorter), len(longer))
        for name in names:
            self.assertTrue(name.startswith("steam"), name)

    def test_limit_arguments_are_stripped_for_both_commands(self) -> None:
        self.assertEqual(plugin_main._strip_command(f"{HOT} 15", plugin_main._HOT_COMMANDS), "15")
        self.assertEqual(
            plugin_main._strip_command(f"{PLAYERS} {TERRARIA}", plugin_main._PLAYERS_COMMANDS),
            TERRARIA,
        )
        self.assertEqual(
            plugin_main._strip_command(PLAYERS_FULL, plugin_main._PLAYERS_COMMANDS), ""
        )


class StripCommandTests(unittest.TestCase):
    def test_removes_the_command_word(self) -> None:
        self.assertEqual(
            plugin_main._strip_command(f"{GAME} {TERRARIA}", plugin_main._GAME_COMMANDS),
            TERRARIA,
        )

    def test_removes_a_leading_slash(self) -> None:
        self.assertEqual(
            plugin_main._strip_command(f"/{GAME} {TERRARIA}", plugin_main._GAME_COMMANDS),
            TERRARIA,
        )

    def test_longest_command_wins(self) -> None:
        # "steam游戏查询" must not leave a stray "查询" behind.
        self.assertEqual(
            plugin_main._strip_command(f"{GAME_QUERY} {TERRARIA}", plugin_main._GAME_COMMANDS),
            TERRARIA,
        )

    def test_bare_command_yields_empty(self) -> None:
        self.assertEqual(plugin_main._strip_command(GAME, plugin_main._GAME_COMMANDS), "")

    def test_preserves_multi_word_names(self) -> None:
        self.assertEqual(
            plugin_main._strip_command(f"{GAME} Baldur's Gate 3", plugin_main._GAME_COMMANDS),
            "Baldur's Gate 3",
        )

    def test_preserves_hyphens_and_symbols(self) -> None:
        self.assertEqual(
            plugin_main._strip_command(f"{GAME} Half-Life 2", plugin_main._GAME_COMMANDS),
            "Half-Life 2",
        )

    def test_deals_argument_is_parsed(self) -> None:
        self.assertEqual(
            plugin_main._strip_command(f"{DEALS} 15", plugin_main._DEALS_COMMANDS), "15"
        )

    def test_empty_message_is_safe(self) -> None:
        self.assertEqual(plugin_main._strip_command("", plugin_main._GAME_COMMANDS), "")


class CommandRegistrationTests(unittest.TestCase):
    def test_commands_are_distinct(self) -> None:
        self.assertEqual(set(plugin_main._GAME_COMMANDS) & set(plugin_main._DEALS_COMMANDS), set())

    def test_no_command_shadows_another_at_a_word_boundary(self) -> None:
        names = set(plugin_main._GAME_COMMANDS) | set(plugin_main._DEALS_COMMANDS)
        for outer in names:
            for inner in names:
                if outer != inner:
                    self.assertFalse(
                        outer.startswith(inner + " "),
                        f"{inner!r} shadows {outer!r} at a word boundary",
                    )

    def test_longer_names_are_listed_first(self) -> None:
        # _strip_command takes the first prefix match, so ordering matters.
        for names in (plugin_main._GAME_COMMANDS, plugin_main._DEALS_COMMANDS):
            self.assertEqual(list(names), sorted(names, key=len, reverse=True))

    def test_aliases_cover_the_documented_commands(self) -> None:
        for name in (GAME, GAME_QUERY, PRICE, PRICE2):
            self.assertIn(name, plugin_main._GAME_COMMANDS)
        for name in (DEALS, DEALS2):
            self.assertIn(name, plugin_main._DEALS_COMMANDS)


class StoreLinkDeliveryTests(unittest.TestCase):
    """A URL drawn inside an image cannot be tapped.

    Drawing the link on the card is not enough: the user would have to retype
    it. The link must also be sent as a text message, which clients turn into a
    clickable link.
    """

    def test_card_handler_emits_a_text_link(self) -> None:
        source = MAIN_SOURCE
        # The link message is built from the card's own store_url.
        self.assertIn("card.store_url", source)

    def test_link_text_is_a_separate_plain_result(self) -> None:
        plugin = plugin_main
        card = plugin.GameCard(
            appid=105600,
            name="Terraria",
            price=None,
            reviews=None,
        )
        self.assertTrue(card.store_url.endswith("/app/105600/"))

        class _Event:
            def plain_result(self, text):
                return ("text", text)

            def chain_result(self, chain):
                return ("chain", chain)

        async def _fake_render(_card):
            return b"\x89PNG\r\n\x1a\n"

        async def drive():
            handler = plugin.SteamDealCardPlugin.__new__(plugin.SteamDealCardPlugin)

            class _Svc:
                render_game = staticmethod(_fake_render)

            handler.service = _Svc()
            out = []
            async for result in handler._render_card(_Event(), card):
                out.append(result)
            return out

        results = asyncio.run(drive())
        kinds = [r[0] for r in results]
        self.assertIn("chain", kinds, "the image must still be sent")
        self.assertIn("text", kinds, "the link must be sent as clickable text")
        # Both an image and the link, in that order.
        self.assertEqual(kinds, ["chain", "text"])
        self.assertIn("Terraria", results[1][1])
        self.assertIn(card.store_url, results[1][1])

    def test_render_failure_still_includes_the_link(self) -> None:
        # The text fallback already carries the URL, so nothing is lost.
        plugin = plugin_main
        card = plugin.GameCard(105600, "Terraria", None, None)
        text = plugin._card_as_text(card)
        self.assertIn(card.store_url, text)


class MetadataTests(unittest.TestCase):
    def test_metadata_matches_the_runtime_version(self) -> None:
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("name: astrbot_plugin_steam_deal_card", metadata)
        self.assertIn(f"version: {plugin_main.PLUGIN_VERSION}", metadata)
        self.assertIn("author: YouYi5213", metadata)
        self.assertIn("YouYi5213/astrbot_plugin_steam_deal_card", metadata)

    def test_runtime_repository_matches_metadata(self) -> None:
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn(plugin_main.PLUGIN_REPOSITORY, metadata)

    def test_required_release_files_exist(self) -> None:
        for filename in ("main.py", "metadata.yaml", "requirements.txt", "README.md"):
            self.assertTrue((ROOT / filename).is_file(), f"{filename} is missing")

    def test_config_schema_is_valid_json(self) -> None:
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("timeout_seconds", "country", "language", "history_country", "max_deals"):
            self.assertIn(key, schema)
            self.assertIn("default", schema[key])

    def test_config_defaults_match_the_service_fallbacks(self) -> None:
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["country"]["default"], "CN")
        self.assertEqual(schema["language"]["default"], "schinese")
        self.assertEqual(schema["history_country"]["default"], "cn")
        self.assertEqual(schema["max_deals"]["default"], 10)


if __name__ == "__main__":
    unittest.main()

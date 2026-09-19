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

    root = types.ModuleType("astrbot")
    root.api = api

    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


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

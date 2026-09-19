"""Regression tests for the AstrBot command binding contract.

These guard constraints that are invisible at runtime until a user types a
multi-word game name, at which point the name would be silently truncated.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def _main_tree() -> ast.Module:
    """Parse main.py into an AST.

    Returns:
        The parsed module.
    """
    return ast.parse(MAIN_SOURCE)


def _handler(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    """Find an async handler by name.

    Args:
        tree: Parsed module.
        name: Function name to find.

    Returns:
        The matching function definition.

    Raises:
        StopIteration: If the handler is absent.
    """
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


class GreedyStrBindingTests(unittest.TestCase):
    def test_main_does_not_postpone_annotations(self) -> None:
        # AstrBot inspects handler signatures with eval_str=True and compares the
        # query annotation against the GreedyStr class object. PEP 563 string
        # annotations break that identity check.
        tree = _main_tree()
        self.assertFalse(
            any(
                isinstance(node, ast.ImportFrom) and node.module == "__future__"
                for node in tree.body
            ),
            "main.py must not use `from __future__ import annotations`",
        )

    def test_game_query_annotation_is_the_runtime_greedy_str(self) -> None:
        handler = _handler(_main_tree(), "steam_game_command")
        query = next(argument for argument in handler.args.args if argument.arg == "query")
        self.assertIsInstance(query.annotation, ast.Name)
        self.assertEqual(query.annotation.id, "GreedyStr")

    def test_game_query_has_no_default_value(self) -> None:
        # A default makes AstrBot treat the parameter as a plain value instead of
        # a greedy string, truncating "黑神话 悟空" to "黑神话".
        handler = _handler(_main_tree(), "steam_game_command")
        query = next(argument for argument in handler.args.args if argument.arg == "query")
        positional = [argument.arg for argument in handler.args.args if argument.arg != "self"]
        defaulted = positional[len(positional) - len(handler.args.defaults) :]
        self.assertNotIn(query.arg, defaulted)

    def test_deals_limit_stays_a_plain_int(self) -> None:
        handler = _handler(_main_tree(), "steam_deals_command")
        limit = next(argument for argument in handler.args.args if argument.arg == "limit")
        self.assertIsInstance(limit.annotation, ast.Name)
        self.assertEqual(limit.annotation.id, "int")

    def test_greedy_str_is_imported_from_the_framework(self) -> None:
        tree = _main_tree()
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "astrbot.core.star.filter.command"
            and any(alias.name == "GreedyStr" for alias in node.names)
            for node in ast.walk(tree)
        )
        self.assertTrue(imported)


class CommandRegistrationTests(unittest.TestCase):
    def _command_names(self, handler_name: str) -> tuple[str, set[str]]:
        """Read the command name and aliases off a handler decorator.

        Args:
            handler_name: Handler function name.

        Returns:
            The primary command name and its alias set.
        """
        handler = _handler(_main_tree(), handler_name)
        decorator = next(
            node
            for node in handler.decorator_list
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "command"
        )
        name = decorator.args[0].value
        aliases = set()
        for keyword in decorator.keywords:
            if keyword.arg == "alias" and isinstance(keyword.value, ast.Set):
                aliases = {element.value for element in keyword.value.elts}
        return name, aliases

    def test_game_command_and_aliases(self) -> None:
        name, aliases = self._command_names("steam_game_command")
        self.assertEqual(name, "steam游戏")
        self.assertIn("steam查价", aliases)
        self.assertIn("steam价格", aliases)

    def test_deals_command_and_aliases(self) -> None:
        name, aliases = self._command_names("steam_deals_command")
        self.assertEqual(name, "steam打折")
        self.assertIn("steam特惠", aliases)
        self.assertIn("steam促销", aliases)

    def test_the_two_commands_do_not_share_a_name(self) -> None:
        game_name, game_aliases = self._command_names("steam_game_command")
        deals_name, deals_aliases = self._command_names("steam_deals_command")
        game_all = {game_name} | game_aliases
        deals_all = {deals_name} | deals_aliases
        self.assertEqual(game_all & deals_all, set())

    def test_no_command_name_shadows_another_at_a_word_boundary(self) -> None:
        # AstrBot accepts a command when the message is the name itself or starts
        # with `name + " "`. A name may therefore be a character prefix of
        # another ("steam游戏" / "steam游戏查询") as long as it is never a prefix
        # at a word boundary, which would make the longer name unreachable.
        names = set()
        for handler in ("steam_game_command", "steam_deals_command"):
            name, aliases = self._command_names(handler)
            names |= {name} | aliases
        for outer in names:
            for inner in names:
                if outer != inner:
                    self.assertFalse(
                        outer.startswith(inner + " "),
                        f"{inner!r} shadows {outer!r} at a word boundary",
                    )


class MetadataTests(unittest.TestCase):
    def test_metadata_matches_the_runtime_version(self) -> None:
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("name: astrbot_plugin_steam_deal_card", metadata)
        self.assertIn("version: 1.0.0", metadata)
        self.assertIn("astrbot_plugin_steam_deal_card", metadata)

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

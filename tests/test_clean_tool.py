"""Tests for tools/clean.py.

The tool deletes files, so its safety properties matter more than its
convenience: it must never target real source, and must never reach into the
other projects that share the workspace.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clean_tool", CHECKOUT / "tools" / "clean.py")
assert SPEC and SPEC.loader
clean_tool = importlib.util.module_from_spec(SPEC)
sys.modules["clean_tool"] = clean_tool
SPEC.loader.exec_module(clean_tool)


class CleanToolSafetyTests(unittest.TestCase):
    """The tool must not delete anything it was not meant to."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.checkout = self.root / "astrbot_plugin_steam_deal_card"
        self.checkout.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, path: Path, text: str = "x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_real_source_files_are_never_targets(self) -> None:
        # These live at the package root and start with an underscore, so a
        # naive "_*" glob would match them.
        self._write(self.checkout / "__init__.py")
        self._write(self.checkout / "_conf_schema.json", "{}")
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [])

    def test_scratch_files_at_the_root_are_targets(self) -> None:
        scratch = self._write(self.root / "_probe.py")
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [scratch])

    def test_other_projects_in_the_workspace_are_untouched(self) -> None:
        # The workspace holds unrelated projects; their caches are not ours.
        other_cache = self.root / "astrbot_plugin_someone_else" / "__pycache__"
        other_cache.mkdir(parents=True)
        self._write(other_cache / "mod.cpython-312.pyc")
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [])

    def test_caches_inside_this_checkout_are_targets(self) -> None:
        cache = self.checkout / "sub" / "__pycache__"
        cache.mkdir(parents=True)
        self._write(cache / "mod.cpython-312.pyc")
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [cache])

    def test_a_cache_inside_another_target_is_not_listed_twice(self) -> None:
        outer = self.checkout / "__pycache__"
        inner = outer / "nested" / "__pycache__"
        inner.mkdir(parents=True)
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [outer])

    def test_the_scratch_directory_is_removed_whole(self) -> None:
        scratch_dir = self.root / "_scratch"
        self._write(scratch_dir / "probe.py")
        found = clean_tool.find_targets(self.root, self.checkout)
        self.assertEqual(found, [scratch_dir])

    def test_dry_run_deletes_nothing(self) -> None:
        scratch = self._write(self.root / "_probe.py")
        code = clean_tool.main(["--dry-run", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertTrue(scratch.exists())

    def test_cleaning_removes_scratch_and_leaves_source(self) -> None:
        scratch = self._write(self.root / "_probe.py")
        source = self._write(self.checkout / "__init__.py")
        code = clean_tool.main(["--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertFalse(scratch.exists())
        self.assertTrue(source.exists())

    def test_an_empty_tree_reports_nothing_to_do(self) -> None:
        self.assertEqual(clean_tool.main(["--root", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Remove development scratch and cache artifacts.

Nothing here is produced at runtime: the plugin keeps no on-disk cache, so a
deployed copy only ever contains source plus Python's own ``__pycache__``. This
script exists for the development tree, where one-off probe scripts and rendered
sample cards pile up.

Run it from anywhere:

    python tools/clean.py            # remove scratch and caches
    python tools/clean.py --dry-run  # list what would go
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Regenerable build/test artifacts, removed wherever they appear in the tree.
CACHE_DIRS = ("__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache")

# Scratch files live in the workspace root, beside the checkout rather than
# inside it, and are named with a leading underscore so .gitignore can cover
# them. Only the root is scanned, never recursively.
SCRATCH_GLOBS = ("_*.py", "_*.txt", "_*.png", "_*.json")
SCRATCH_DIRS = ("_scratch",)

# Real source files that happen to start with an underscore. Named explicitly
# so a mis-set --root can never delete them.
PROTECTED = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "_conf_schema.json",
        "_conf_schema.json.example",
    }
)


def find_targets(root: Path, checkout: Path) -> list[Path]:
    """Collect everything that should be removed.

    Scope is deliberately narrow: caches inside this checkout, and scratch files
    at the workspace root. The workspace holds other unrelated projects, and
    this tool has no business deleting inside them.

    Args:
        root: Workspace root that holds the checkout and any scratch files.
        checkout: This plugin's directory.

    Returns:
        Existing paths to delete, parents before children.
    """
    targets: list[Path] = []
    for path in checkout.rglob("*"):
        if path.is_dir() and path.name in CACHE_DIRS:
            targets.append(path)

    for pattern in SCRATCH_GLOBS:
        targets.extend(p for p in root.glob(pattern) if p.name not in PROTECTED)
    for name in SCRATCH_DIRS:
        candidate = root / name
        if candidate.is_dir():
            targets.append(candidate)

    # Drop anything nested inside another target, so nothing is removed twice.
    unique: list[Path] = []
    for path in sorted(set(targets), key=lambda p: len(p.parts)):
        if not any(path.is_relative_to(kept) for kept in unique):
            unique.append(path)
    return unique


def _size_of(path: Path) -> int:
    """Total byte size of a file or directory.

    Args:
        path: File or directory to measure.

    Returns:
        The size in bytes.
    """
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    checkout = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the paths without deleting anything",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=checkout.parent,
        help="workspace root to clean (defaults to the checkout's parent)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    targets = find_targets(root, checkout)
    if not targets:
        print(f"没有需要清理的内容（{root}）")
        return 0

    total = 0
    for path in targets:
        total += _size_of(path)
        try:
            label = path.relative_to(root)
        except ValueError:
            label = path
        print(f"{'[预览] ' if args.dry_run else '删除   '}{label}")
        if not args.dry_run:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)

    verb = "可释放" if args.dry_run else "已释放"
    print(f"\n{len(targets)} 项，{verb} {total / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

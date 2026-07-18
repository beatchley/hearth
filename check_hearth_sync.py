#!/usr/bin/env python3
"""
Checks whether ~/hearth and ~/pathway-portal/hearth are in sync.

Background: hearth/ is developed as two copies — a standalone repo
(~/hearth) and a duplicated directory inside pathway-portal
(~/pathway-portal/hearth), which is not a nested git repo, just a plain
directory kept in sync by hand. On 2026-07-18 a real fix (the
training_comment_needs_response resolution path) landed in the
pathway-portal copy but was never copied back to the standalone repo,
and stayed silently out of sync for a full day before being caught. This
script is a small, read-only tool to catch that class of drift going
forward.

What it does:
    1. Recursively finds every .py and .md file under both hearth/
       directories.
    2. Skips whatever each location's own .gitignore already excludes
       (venv/, __pycache__/, build/, etc.) — reusing the existing
       exclusion patterns rather than inventing new ones, and always
       skipping .git.
    3. Compares files that exist in both locations byte-for-byte, and
       reports any that differ, along with which side has the newer
       mtime.
    4. Reports any file that exists on only one side.
    5. Exits 0 if everything in sync, 1 otherwise — safe to use as a
       pre-commit check later if desired (not wired into anything here).

Reads files and mtimes only. Never writes, moves, or deletes anything.

Run: python3 check_hearth_sync.py
"""

import fnmatch
import os
import sys
from pathlib import Path

HEARTH_DIR = Path.home() / "hearth"
PATHWAY_HEARTH_DIR = Path.home() / "pathway-portal" / "hearth"

COMPARE_EXTENSIONS = (".py", ".md")

ALWAYS_SKIP_DIRNAMES = {".git"}


def _load_gitignore_patterns(root: Path):
    """Parse root/.gitignore into (dir_only_patterns, general_patterns).

    Handles the simple, non-anchored, non-negated patterns actually used in
    this repo's .gitignore files (e.g. "venv/", "*.db", "__pycache__/").
    Good enough for this purpose — not a full gitignore implementation.
    """
    gitignore_path = root / ".gitignore"
    dir_only = set()
    general = set()
    if not gitignore_path.is_file():
        return dir_only, general

    for line in gitignore_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("/"):
            dir_only.add(line[:-1])
        else:
            general.add(line)
    return dir_only, general


def _matches_any(name, patterns):
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _collect_files(root: Path):
    """Return {relative_path: absolute_path} for every .py/.md file under
    root, honoring root's own .gitignore for directory/file exclusions."""
    dir_only_patterns, general_patterns = _load_gitignore_patterns(root)
    results = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in ALWAYS_SKIP_DIRNAMES
            and not _matches_any(d, dir_only_patterns)
            and not _matches_any(d, general_patterns)
        ]
        for fname in filenames:
            if not fname.endswith(COMPARE_EXTENSIONS):
                continue
            if _matches_any(fname, general_patterns):
                continue
            abs_path = Path(dirpath) / fname
            rel_path = abs_path.relative_to(root)
            results[rel_path.as_posix()] = abs_path

    return results


def main():
    if not HEARTH_DIR.is_dir():
        print(f"ERROR: {HEARTH_DIR} does not exist or is not a directory.")
        return 2
    if not PATHWAY_HEARTH_DIR.is_dir():
        print(f"ERROR: {PATHWAY_HEARTH_DIR} does not exist or is not a directory.")
        return 2

    hearth_files = _collect_files(HEARTH_DIR)
    pathway_files = _collect_files(PATHWAY_HEARTH_DIR)

    hearth_only = sorted(set(hearth_files) - set(pathway_files))
    pathway_only = sorted(set(pathway_files) - set(hearth_files))
    shared = sorted(set(hearth_files) & set(pathway_files))

    differing = []
    for rel_path in shared:
        h_path = hearth_files[rel_path]
        p_path = pathway_files[rel_path]
        h_bytes = h_path.read_bytes()
        p_bytes = p_path.read_bytes()
        if h_bytes != p_bytes:
            h_mtime = h_path.stat().st_mtime
            p_mtime = p_path.stat().st_mtime
            if h_mtime > p_mtime:
                direction = "newer in ~/hearth, stale in ~/pathway-portal/hearth"
            elif p_mtime > h_mtime:
                direction = "newer in ~/pathway-portal/hearth, stale in ~/hearth"
            else:
                direction = "differs, mtimes equal — direction unclear"
            differing.append((rel_path, direction))

    print("=" * 72)
    print(f"hearth sync check — {HEARTH_DIR} vs {PATHWAY_HEARTH_DIR}")
    print("=" * 72)
    print(f"Compared {len(shared)} shared .py/.md file(s) "
          f"({len(hearth_files)} in ~/hearth, {len(pathway_files)} in "
          f"~/pathway-portal/hearth).\n")

    problems = 0

    if differing:
        problems += len(differing)
        print(f"OUT OF SYNC — {len(differing)} file(s) differ:")
        for rel_path, direction in differing:
            print(f"  {rel_path}")
            print(f"    {direction}")
        print()

    if hearth_only:
        problems += len(hearth_only)
        print(f"Only in ~/hearth ({len(hearth_only)}):")
        for rel_path in hearth_only:
            print(f"  {rel_path}")
        print()

    if pathway_only:
        problems += len(pathway_only)
        print(f"Only in ~/pathway-portal/hearth ({len(pathway_only)}):")
        for rel_path in pathway_only:
            print(f"  {rel_path}")
        print()

    if problems == 0:
        print("IN SYNC — every shared .py/.md file is byte-identical, "
              "no one-sided files found.")
        return 0

    print(f"{problems} issue(s) found — see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

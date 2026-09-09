#!/usr/bin/env python3
"""Enforce CLAUDE.md convention 8: code carries code, docs carry explanation.

Ruff cannot check either rule this gates — comment-block length, and the
length of a docstring's prose summary — so without this they are on the
honor system, which is exactly how the repo accumulated the walls of
prose convention 8 was written to stop.

Existing offenders are listed in the baseline beside this file. A file in
it may not get worse; a file outside it may not have a single violation.
The baseline only ever shrinks: delete a line once its file is clean.

Run: uv run python scripts/check_comment_density.py [--regenerate]
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = pathlib.Path(__file__).resolve().parent / "comment-density-baseline.txt"

MAX_COMMENT_RUN = 3
MAX_SUMMARY_LINES = 3

SOURCES: tuple[tuple[str, str, str], ...] = (
    ("backend/app", "**/*.py", "#"),
    ("tools", "**/*.py", "#"),
    ("tests", "**/*.py", "#"),
    ("frontend/src", "**/*.ts", "//"),
    ("frontend/src", "**/*.tsx", "//"),
)

_SECTIONS = ("Args:", "Returns:", "Raises:", "Yields:", "Attributes:", "Examples:")


def comment_runs(text: str, marker: str) -> list[tuple[int, int]]:
    """
    Find every run of consecutive whole-line comments.

    Args:
        text (str): The file's contents.
        marker (str): The line-comment marker, `#` or `//`.

    Returns:
        list[tuple[int, int]]: One `(start_line, length)` per run.
    """
    found: list[tuple[int, int]] = []
    length = start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(marker):
            start = number if length == 0 else start
            length += 1
            continue
        if length:
            found.append((start, length))
        length = 0
    if length:
        found.append((start, length))
    return found


def summary_lines(docstring: str) -> int:
    """
    Count a docstring's prose lines, stopping at the first section header.

    Args:
        docstring (str): The raw docstring.

    Returns:
        int: Non-blank lines before `Args:`/`Returns:`/`Raises:`/etc.
    """
    count = 0
    for line in docstring.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(_SECTIONS):
            break
        if stripped:
            count += 1
    return count


def long_summaries(path: pathlib.Path, text: str) -> list[tuple[int, int]]:
    """
    Find docstrings whose summary runs past the limit.

    A file that does not parse is skipped rather than failed: `ruff` and
    `ty` already gate syntax, and reporting it twice helps nobody.

    Args:
        path (pathlib.Path): The file, for the error message.
        text (str): Its contents.

    Returns:
        list[tuple[int, int]]: One `(line, summary_length)` per offender.
    """
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, kinds):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if docstring and (length := summary_lines(docstring)) > MAX_SUMMARY_LINES:
            found.append((node.lineno, length))
    return found


def violations() -> tuple[Counter[str], list[str]]:
    """
    Scan every source tree for both violations.

    Returns:
        tuple[Counter[str], list[str]]: Per-file violation counts, and one
            human-readable message per violation.
    """
    counts: Counter[str] = Counter()
    messages: list[str] = []
    for root, pattern, marker in SOURCES:
        for path in sorted((REPO / root).glob(pattern)):
            relative = path.relative_to(REPO).as_posix()
            text = path.read_text(encoding="utf-8")
            for line, length in comment_runs(text, marker):
                if length > MAX_COMMENT_RUN:
                    counts[relative] += 1
                    messages.append(
                        f"{relative}:{line}: {length} consecutive comment lines "
                        f"(max {MAX_COMMENT_RUN}) — move the explanation to docs/"
                    )
            if path.suffix == ".py":
                for line, length in long_summaries(path, text):
                    counts[relative] += 1
                    messages.append(
                        f"{relative}:{line}: docstring summary is {length} lines "
                        f"(max {MAX_SUMMARY_LINES}) — move the explanation to docs/"
                    )
    return counts, messages


def read_baseline() -> dict[str, int]:
    """
    Load the allowed per-file violation counts.

    Returns:
        dict[str, int]: Path to allowance, empty if the file is absent.
    """
    if not BASELINE.exists():
        return {}
    allowed: dict[str, int] = {}
    for raw in BASELINE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            path, _, count = line.rpartition(" ")
            allowed[path.strip()] = int(count)
    return allowed


def write_baseline(counts: Counter[str]) -> None:
    """
    Rewrite the baseline from the current tree.

    Args:
        counts (Counter[str]): Per-file violation counts.
    """
    header = (
        "# Files still carrying pre-convention-8 comment walls, with how many\n"
        "# violations each has. Generated by scripts/check_comment_density.py.\n"
        "#\n"
        "# This list may only SHRINK. Do not add a line to make a new violation\n"
        "# pass — move the explanation into docs/ instead.\n"
    )
    body = "".join(f"{path} {count}\n" for path, count in sorted(counts.items()))
    BASELINE.write_text(header + body, encoding="utf-8")


def main() -> int:
    """
    Check the tree against the baseline.

    Returns:
        int: 0 when clean, 1 when a file is new or worse than baselined.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate", action="store_true", help="Rewrite the baseline.")
    args = parser.parse_args()

    counts, messages = violations()
    if args.regenerate:
        write_baseline(counts)
        print(f"Baseline rewritten: {len(counts)} files, {sum(counts.values())} violations.")
        return 0

    allowed = read_baseline()
    regressions = {path: count for path, count in counts.items() if count > allowed.get(path, 0)}
    if regressions:
        print("Comment density (CLAUDE.md convention 8):\n", file=sys.stderr)
        for message in messages:
            if message.split(":", 1)[0] in regressions:
                print(f"  {message}", file=sys.stderr)
        print(
            "\nCode carries code; explanation belongs in docs/ with a one-line "
            "pointer from the code.",
            file=sys.stderr,
        )
        return 1

    improved = {p: c for p, c in allowed.items() if counts.get(p, 0) < c}
    if improved:
        print(f"{len(improved)} file(s) improved — run with --regenerate to bank it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Merge GN assignment files with deterministic last-file overrides."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
PGO_KEY = "chrome_pgo_phase"
PGO_ASSIGNMENT = re.compile(r"chrome_pgo_phase\s*=\s*[012]\s*(?:#.*)?")
GENERATED_HEADER = "# Generated. Later input files override earlier assignments."


def parse(path: Path, *, require_pgo: bool = False) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}
    pending_comments: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == GENERATED_HEADER:
            continue
        if not line:
            pending_comments.clear()
            continue
        if line.startswith("#"):
            pending_comments.append(line)
            continue
        match = ASSIGNMENT.match(line)
        if not match:
            raise ValueError(f"{path}: unsupported GN line: {raw}")
        key = match.group(1)
        if require_pgo and key == PGO_KEY and not PGO_ASSIGNMENT.fullmatch(line):
            raise ValueError(f"{path}: chrome_pgo_phase must be a literal 0, 1 or 2")
        if key not in values:
            order.append(key)
        comments = "\n".join(pending_comments)
        values[key] = f"{comments}\n{line}" if comments else line
        pending_comments.clear()
    if require_pgo and PGO_KEY not in values:
        raise ValueError(f"{path}: required chrome_pgo_phase assignment is missing")
    return order, values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-profile", choices=("fast", "release"),
                        help="override ThinLTO optimization for a build")
    parser.add_argument("--preserve-pgo-from", type=Path,
                        help="preserve required literal chrome_pgo_phase from previously verified restored args")
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    preserved_pgo = None
    if args.preserve_pgo_from:
        try:
            _, values = parse(args.preserve_pgo_from, require_pgo=True)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        preserved_pgo = values[PGO_KEY]

    order: list[str] = []
    merged: dict[str, str] = {}
    for path in args.inputs:
        file_order, values = parse(path)
        for key in file_order:
            if key not in merged:
                order.append(key)
            merged[key] = values[key]

    if preserved_pgo is not None:
        if PGO_KEY not in merged:
            order.append(PGO_KEY)
        merged[PGO_KEY] = preserved_pgo

    if args.build_profile:
        key = "thin_lto_enable_optimizations"
        if key not in merged:
            order.append(key)
        value = "false" if args.build_profile == "fast" else "true"
        merged[key] = f"{key} = {value}"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = [GENERATED_HEADER]
    body.extend(merged[key] for key in order)
    content = "\n".join(body) + "\n"
    if args.output.exists() and args.output.read_text(encoding="utf-8") == content:
        print(f"unchanged {args.output} with {len(order)} GN assignments")
        return 0
    args.output.write_text(content, encoding="utf-8")
    print(f"wrote {args.output} with {len(order)} GN assignments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Merge GN assignment files with deterministic last-file overrides."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def parse(path: Path) -> tuple[list[str], dict[str, str]]:
    order: list[str] = []
    values: dict[str, str] = {}
    pending_comments: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
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
        if key not in values:
            order.append(key)
        comments = "\n".join(pending_comments)
        values[key] = f"{comments}\n{line}" if comments else line
        pending_comments.clear()
    return order, values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()

    order: list[str] = []
    merged: dict[str, str] = {}
    for path in args.inputs:
        file_order, values = parse(path)
        for key in file_order:
            if key not in merged:
                order.append(key)
            merged[key] = values[key]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = ["# Generated. Later input files override earlier assignments."]
    body.extend(merged[key] for key in order)
    args.output.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"wrote {args.output} with {len(order)} GN assignments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

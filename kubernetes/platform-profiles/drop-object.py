#!/usr/bin/env python3
"""Remove one generated object that is owned by another profile."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--removed-output", type=Path)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    kept = []
    removed_documents = []
    removed = 0
    for document in args.input.read_text().split("\n---\n"):
        if not document.strip():
            continue
        parsed = yaml.safe_load(document)
        identity = (
            parsed.get("kind"),
            parsed.get("metadata", {}).get("name"),
        ) if isinstance(parsed, dict) else (None, None)
        if identity == (args.kind, args.name):
            removed += 1
            removed_documents.append(document.rstrip())
        else:
            kept.append(document.rstrip())
    if removed != 1:
        raise SystemExit(
            f"expected one {args.kind}/{args.name}, removed {removed}"
        )
    args.output.write_text("\n---\n".join(kept) + "\n")
    if args.removed_output:
        args.removed_output.write_text(
            "\n---\n".join(removed_documents) + "\n"
        )


if __name__ == "__main__":
    main()

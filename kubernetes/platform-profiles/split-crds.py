#!/usr/bin/env python3
"""Split rendered CustomResourceDefinitions into Helm's pre-install phase."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--crds", type=Path, required=True)
    args = parser.parse_args()

    resources = []
    crds = []
    for document in args.input.read_text().split("\n---\n"):
        if not document.strip():
            continue
        parsed = yaml.safe_load(document)
        if not isinstance(parsed, dict):
            continue
        target = crds if parsed.get("kind") == "CustomResourceDefinition" else resources
        target.append(document.rstrip())

    if not crds:
        raise SystemExit(f"no CRDs found in {args.input}")
    args.resources.write_text("\n---\n".join(resources) + "\n")
    args.crds.write_text("\n---\n".join(crds) + "\n")


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""Replace every locked image tag in a rendered manifest with its digest."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-envoy-concurrency", type=int)
    args = parser.parse_args()

    text = args.input.read_text()
    replacements = []
    for line in args.lock.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        image, digest = line.split("\t", 1)
        replacements.append((image, f"{image}@{digest}"))

    for image, pinned in replacements:
        text = text.replace(image, pinned)

    if args.metadata_envoy_concurrency is not None:
        marker = """      containers:
      - args:
        - /etc/envoy/envoy-config.yaml
"""
        replacement = marker + (
            "        - --concurrency\n"
            f"        - \"{args.metadata_envoy_concurrency}\"\n"
        )
        if text.count(marker) != 1:
            raise SystemExit("metadata-envoy argument marker is not unique")
        text = text.replace(marker, replacement)

    stale = [
        image
        for image, _ in replacements
        if image in text and f"{image}@sha256:" not in text
    ]
    if stale:
        raise SystemExit(f"unreplaced image references: {stale}")
    args.output.write_text(text)


if __name__ == "__main__":
    main()

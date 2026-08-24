#!/usr/bin/env python3
"""Split rendered documents below Helm's per-file chart size limit."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=4_000_000)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for old in args.output_dir.glob("resources-*.yaml"):
        old.unlink()

    chunks = []
    current = []
    size = 0
    for document in args.input.read_text().split("\n---\n"):
        document = document.strip()
        if not document:
            continue
        encoded_size = len(document.encode()) + 5
        if encoded_size > args.max_bytes:
            raise SystemExit("one rendered object exceeds the Helm file limit")
        if current and size + encoded_size > args.max_bytes:
            chunks.append(current)
            current = []
            size = 0
        current.append(document)
        size += encoded_size
    if current:
        chunks.append(current)

    for index, documents in enumerate(chunks, start=1):
        output = args.output_dir / f"resources-{index:03d}.yaml"
        output.write_text("\n---\n".join(documents) + "\n")


if __name__ == "__main__":
    main()


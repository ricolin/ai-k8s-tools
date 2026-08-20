from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a licensed SDXL imagefolder dataset")
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--stages", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    stages = {value.strip() for value in args.stages.split(",") if value.strip()}
    if not stages or not stages <= {"A", "B-detail", "A-replay", "C-watercolor", "C-complex-detail", "C-retention"}:
        raise SystemExit("invalid image training stages")
    source_root = args.dataset_root.resolve()
    output = args.output.resolve()
    if source_root == output or source_root in output.parents or output in source_root.parents:
        raise SystemExit("prepared dataset must not overlap its immutable source")
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, raw in enumerate(args.manifest.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        record = json.loads(raw)
        required = {"id", "path", "caption", "source", "license", "permission_confirmed", "sha256", "stage", "split"}
        missing = required - set(record)
        if missing:
            raise SystemExit(f"manifest line {line_number} is missing {sorted(missing)}")
        if record["id"] in seen_ids:
            raise SystemExit(f"duplicate dataset id: {record['id']}")
        seen_ids.add(record["id"])
        if record["permission_confirmed"] is not True or not str(record["license"]).strip():
            raise SystemExit(f"dataset permission is incomplete: {record['id']}")
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe dataset path: {relative}")
        image = (source_root / relative).resolve()
        if source_root not in image.parents or not image.is_file():
            raise SystemExit(f"dataset image is missing: {relative}")
        if sha256_file(image) != record["sha256"]:
            raise SystemExit(f"dataset image digest mismatch: {relative}")
        with Image.open(image) as opened:
            opened.verify()
        if record["stage"] in stages and record["split"] == "train":
            records.append({**record, "source_path": image})
    if not records:
        raise SystemExit("no training images match the selected stages")

    output.mkdir(parents=True, exist_ok=False)
    metadata: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for record in records:
        repeats = int(record.get("sampling_weight", 1))
        if not 1 <= repeats <= 20:
            raise SystemExit(f"invalid sampling weight: {record['id']}")
        suffix = record["source_path"].suffix.lower()
        for repeat in range(repeats):
            name = f"{record['id']}-{repeat:02d}{suffix}"
            destination = output / name
            shutil.copyfile(record["source_path"], destination)
            metadata.append({"file_name": name, "text": str(record["caption"])})
            evidence.append(
                {
                    "id": record["id"],
                    "stage": record["stage"],
                    "file_name": name,
                    "sha256": f"sha256:{sha256_file(destination)}",
                    "source": record["source"],
                    "license": record["license"],
                }
            )
    metadata_path = output / "metadata.jsonl"
    metadata_path.write_bytes(b"".join(canonical_json(record) + b"\n" for record in metadata))
    summary = {
        "schema_version": "1.0.0",
        "status": "PASS",
        "selected_stages": sorted(stages),
        "source_manifest_digest": f"sha256:{sha256_file(args.manifest)}",
        "prepared_records": len(metadata),
        "metadata_digest": f"sha256:{sha256_file(metadata_path)}",
        "files": evidence,
    }
    (output / "prepared-dataset.json").write_bytes(canonical_json(summary) + b"\n")


if __name__ == "__main__":
    main()

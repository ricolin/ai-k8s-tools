from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an ephemeral SDXL foundation with frozen parent LoRA fused")
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--base-digest", required=True)
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--parent-digest", required=True)
    parser.add_argument("--parent-scale", type=float, default=1.0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIFFUSERS_OFFLINE="1")
    if f"sha256:{sha256_tree(args.base)}" != args.base_digest:
        raise SystemExit("base model digest mismatch")
    parent_before = sha256_tree(args.parent)
    if f"sha256:{parent_before}" != args.parent_digest:
        raise SystemExit("parent adapter digest mismatch")
    if args.output.exists():
        raise SystemExit("composed foundation output already exists")

    import torch
    from diffusers import StableDiffusionXLPipeline

    pipeline = StableDiffusionXLPipeline.from_pretrained(
        args.base,
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        variant="fp16",
    )
    pipeline.load_lora_weights(
        args.parent,
        weight_name="pytorch_lora_weights.safetensors",
        adapter_name="frozen-parent",
        local_files_only=True,
    )
    pipeline.set_adapters("frozen-parent", adapter_weights=args.parent_scale)
    pipeline.fuse_lora(adapter_names=["frozen-parent"], lora_scale=1.0)
    pipeline.unload_lora_weights()
    pipeline.save_pretrained(args.output, safe_serialization=True, variant="fp16")
    parent_after = sha256_tree(args.parent)
    if parent_before != parent_after:
        raise SystemExit("parent adapter changed during composition")
    evidence = {
        "schema_version": "1.0.0",
        "base_digest": args.base_digest,
        "parent_adapter_digest": args.parent_digest,
        "parent_scale": args.parent_scale,
        "parent_unchanged": True,
        "composed_foundation_digest": f"sha256:{sha256_tree(args.output)}",
    }
    (args.output / "composition-evidence.json").write_text(json.dumps(evidence, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

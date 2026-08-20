from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a digest-bound SDXL comparison gallery")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", DIFFUSERS_OFFLINE="1")
    prompts_path = Path(config["prompts_path"])
    if f"sha256:{sha256_file(prompts_path)}" != config["prompts_digest"]:
        raise SystemExit("comparison prompt manifest digest mismatch")
    prompts = json.loads(prompts_path.read_text())
    if not isinstance(prompts, list) or not prompts:
        raise SystemExit("comparison prompt manifest must be a non-empty list")
    output = Path(config["output_dir"])
    if output.exists():
        raise SystemExit("generation output already exists")
    output.mkdir(parents=True)

    import torch
    from diffusers import StableDiffusionXLPipeline

    pipeline = StableDiffusionXLPipeline.from_pretrained(
        config["base_path"],
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        variant="fp16",
    ).to("cuda")
    names: list[str] = []
    scales: list[float] = []
    adapter_identities: list[dict[str, Any]] = []
    for adapter in config.get("adapters", []):
        path = Path(adapter["path"])
        weights = path / "pytorch_lora_weights.safetensors"
        observed = f"sha256:{sha256_file(weights)}"
        if observed != adapter["digest"]:
            raise SystemExit(f"adapter digest mismatch: {adapter['name']}")
        pipeline.load_lora_weights(
            path,
            weight_name=weights.name,
            adapter_name=adapter["name"],
            local_files_only=True,
        )
        names.append(adapter["name"])
        scales.append(float(adapter["scale"]))
        adapter_identities.append({"name": adapter["name"], "digest": observed, "scale": float(adapter["scale"])})
    if names:
        pipeline.set_adapters(names, adapter_weights=scales)
    pipeline.set_progress_bar_config(disable=True)

    records: list[dict[str, Any]] = []
    for prompt in prompts:
        identifier = str(prompt["id"])
        if not identifier.replace("-", "").isalnum():
            raise SystemExit(f"unsafe prompt id: {identifier}")
        seed = int(prompt["seed"])
        generator = torch.Generator(device="cuda").manual_seed(seed)
        image = pipeline(
            prompt=str(prompt["prompt"]),
            negative_prompt=str(prompt["negative_prompt"]),
            width=int(prompt["width"]),
            height=int(prompt["height"]),
            num_inference_steps=int(prompt["steps"]),
            guidance_scale=float(prompt["guidance"]),
            generator=generator,
        ).images[0]
        filename = f"{config['release_name']}-{identifier}.png"
        destination = output / filename
        image.save(destination, format="PNG")
        records.append(
            {
                **prompt,
                "release_name": config["release_name"],
                "file": filename,
                "sha256": f"sha256:{sha256_file(destination)}",
                "base_digest": config["base_digest"],
                "adapters": adapter_identities,
            }
        )
    (output / "metadata.json").write_bytes(canonical_json(records) + b"\n")


if __name__ == "__main__":
    main()

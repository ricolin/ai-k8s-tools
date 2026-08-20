from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
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


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def digest_valid(value: str) -> bool:
    return value.startswith("sha256:") and len(value) == 71 and all(c in "0123456789abcdef" for c in value[7:])


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    require(config.get("schema_version") == "1.0.0", "unsupported image training config")
    require(config.get("stage") in {"A", "B-detail", "B-impressionism"}, "stage must be A, B-detail, or B-impressionism")
    for field in ("base_path", "prepared_dataset_path", "output_dir"):
        require(isinstance(config.get(field), str) and config[field].startswith("/"), f"{field} must be absolute")
    for field in ("base_digest", "prepared_dataset_digest"):
        require(digest_valid(str(config.get(field, ""))), f"{field} must be a sha256 digest")
    parent = config.get("parent_adapter_path")
    if config["stage"] == "A":
        require(parent in {None, ""}, "Release A cannot have a parent")
    else:
        require(isinstance(parent, str) and parent.startswith("/"), "Release B parent path is required")
        require(digest_valid(str(config.get("parent_adapter_digest", ""))), "Release B parent digest is required")
    training = config.get("training", {})
    for field in ("gpu_count", "max_steps", "checkpoint_steps", "resolution", "rank", "batch_size", "gradient_accumulation"):
        require(int(training.get(field, 0)) > 0, f"training.{field} must be positive")
    for field in ("learning_rate", "parent_scale"):
        require(isinstance(training.get(field), (int, float)), f"training.{field} must be numeric")
    output = Path(config["output_dir"]).resolve()
    inputs = [Path(config["base_path"]).resolve(), Path(config["prepared_dataset_path"]).resolve()]
    if parent:
        inputs.append(Path(parent).resolve())
    for source in inputs:
        require(output != source and output not in source.parents, "output cannot contain an immutable input")
        require(source not in output.parents, "output cannot be nested under an immutable input")
    return config


def verify_prepared_dataset(dataset: Path, expected_digest: str) -> dict[str, Any]:
    summary_path = dataset / "prepared-dataset.json"
    require(summary_path.is_file(), "prepared dataset summary is missing")
    require(f"sha256:{sha256_file(summary_path)}" == expected_digest, "prepared dataset digest mismatch")
    summary = json.loads(summary_path.read_text())
    metadata = dataset / "metadata.jsonl"
    require(f"sha256:{sha256_file(metadata)}" == summary["metadata_digest"], "prepared metadata digest mismatch")
    for record in summary["files"]:
        image = dataset / record["file_name"]
        require(image.is_file(), f"prepared image is missing: {record['file_name']}")
        require(f"sha256:{sha256_file(image)}" == record["sha256"], f"prepared image digest mismatch: {record['file_name']}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline SDXL A/B LoRA stage launcher")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = validate_config(json.loads(args.config.read_text()))
    training = config["training"]
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        DIFFUSERS_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
    )
    base = Path(config["base_path"])
    dataset = Path(config["prepared_dataset_path"])
    output = Path(config["output_dir"])
    require(f"sha256:{sha256_tree(base)}" == config["base_digest"], "base model digest mismatch")
    verify_prepared_dataset(dataset, config["prepared_dataset_digest"])
    require(not output.exists(), "training output already exists")
    output.mkdir(parents=True)
    training_base = base
    parent_before = None
    if config["stage"] in {"B-detail", "B-impressionism"}:
        parent = Path(config["parent_adapter_path"])
        parent_before = sha256_tree(parent)
        require(f"sha256:{parent_before}" == config["parent_adapter_digest"], "parent adapter digest mismatch")
        training_base = output / "ephemeral-composed-foundation"
        subprocess.run(
            [
                sys.executable,
                "/opt/ai-build-tools-image/compose_parent.py",
                "--base",
                str(base),
                "--base-digest",
                config["base_digest"],
                "--parent",
                str(parent),
                "--parent-digest",
                config["parent_adapter_digest"],
                "--parent-scale",
                str(training["parent_scale"]),
                "--output",
                str(training_base),
            ],
            check=True,
        )

    trainer_output = output / "adapter"
    warmup_steps = max(1, round(int(training["max_steps"]) * float(training.get("warmup_ratio", 0.05))))
    command = [
        sys.executable,
        "-m",
        "accelerate.commands.launch",
        "--num_machines",
        "1",
        "--num_processes",
        str(training["gpu_count"]),
        "--mixed_precision",
        "bf16",
    ]
    if int(training["gpu_count"]) > 1:
        command.append("--multi_gpu")
    command.extend(
        [
            "/opt/diffusers/examples/advanced_diffusion_training/train_dreambooth_lora_sdxl_advanced.py",
            "--pretrained_model_name_or_path",
            str(training_base),
            "--variant",
            "fp16",
            "--dataset_name",
            str(dataset),
            "--image_column",
            "image",
            "--caption_column",
            "text",
            "--instance_prompt",
            str(config["fallback_prompt"]),
            "--output_dir",
            str(trainer_output),
            "--resolution",
            str(training["resolution"]),
            "--train_batch_size",
            str(training["batch_size"]),
            "--gradient_accumulation_steps",
            str(training["gradient_accumulation"]),
            "--learning_rate",
            str(training["learning_rate"]),
            "--lr_scheduler",
            "cosine",
            "--lr_warmup_steps",
            str(warmup_steps),
            "--max_train_steps",
            str(training["max_steps"]),
            "--checkpointing_steps",
            str(training["checkpoint_steps"]),
            "--checkpoints_total_limit",
            str(training.get("checkpoint_limit", 3)),
            "--rank",
            str(training["rank"]),
            "--mixed_precision",
            "bf16",
            "--seed",
            str(config["seed"]),
            "--gradient_checkpointing",
            "--report_to",
            "tensorboard",
            "--dataloader_num_workers",
            str(training.get("dataloader_workers", 2)),
        ]
    )
    (output / "training-command.json").write_bytes(canonical_json(command) + b"\n")
    subprocess.run(command, check=True)
    adapter = trainer_output / "pytorch_lora_weights.safetensors"
    require(adapter.is_file(), "trainer did not produce the LoRA adapter")
    if parent_before is not None:
        require(parent_before == sha256_tree(Path(config["parent_adapter_path"])), "parent changed during training")
    result = {
        "schema_version": "1.0.0",
        "status": "CANDIDATE",
        "stage": config["stage"],
        "base_digest": config["base_digest"],
        "parent_adapter_digest": config.get("parent_adapter_digest"),
        "parent_unchanged": parent_before is None or parent_before == sha256_tree(Path(config["parent_adapter_path"])),
        "adapter_digest": f"sha256:{sha256_file(adapter)}",
        "adapter_tree_digest": f"sha256:{sha256_tree(trainer_output)}",
        "prepared_dataset_digest": config["prepared_dataset_digest"],
        "gpu_count": training["gpu_count"],
        "effective_global_batch": (
            int(training["gpu_count"]) * int(training["batch_size"]) * int(training["gradient_accumulation"])
        ),
        "config_digest": f"sha256:{hashlib.sha256(canonical_json(config)).hexdigest()}",
    }
    (output / "training-result.json").write_bytes(canonical_json(result) + b"\n")


if __name__ == "__main__":
    main()

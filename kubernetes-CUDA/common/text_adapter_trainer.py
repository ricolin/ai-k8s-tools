from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAGES = {"A", "B", "C"}
TRAINING_SPLITS = {"train"}


class TrainingContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TrainingContractError(message)


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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def require_digest(value: str, field: str) -> None:
    require(
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{field} must be a lowercase sha256 digest",
    )


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), "training config must be a JSON object")
    return value


def resolve_path(value: Any, field: str) -> Path:
    require(isinstance(value, str) and value.startswith("/"), f"{field} must be an absolute path")
    return Path(value).resolve()


def validate_training_config(config: dict[str, Any]) -> dict[str, Any]:
    require(config.get("schema_version") == "1.0.0", "unsupported training config schema")
    stage = config.get("stage")
    require(stage in STAGES, "stage must be A, B, or C")
    for field in ("foundation_path", "tokenizer_path", "dataset_root", "dataset_manifest", "output_dir"):
        resolve_path(config.get(field), field)
    require_digest(str(config.get("foundation_digest", "")), "foundation_digest")
    require_digest(str(config.get("dataset_manifest_digest", "")), "dataset_manifest_digest")
    parent = config.get("parent_adapter_path")
    parent_digest = config.get("parent_adapter_digest")
    if stage == "A":
        require(parent in {None, ""}, "Release A cannot have a parent adapter")
        require(parent_digest in {None, ""}, "Release A cannot have a parent adapter digest")
    else:
        resolve_path(parent, "parent_adapter_path")
        require_digest(str(parent_digest), "parent_adapter_digest")

    training = config.get("training", {})
    required_positive_ints = (
        "expected_gpu_count",
        "max_steps",
        "save_steps",
        "sequence_length",
        "per_device_batch_size",
        "gradient_accumulation_steps",
        "lora_rank",
        "lora_alpha",
    )
    for field in required_positive_ints:
        require(int(training.get(field, 0)) > 0, f"training.{field} must be positive")
    for field in ("learning_rate", "weight_decay", "warmup_ratio", "max_grad_norm", "lora_dropout"):
        require(isinstance(training.get(field), (int, float)), f"training.{field} must be numeric")
    target_modules = training.get("target_modules")
    require(isinstance(target_modules, list) and target_modules, "training.target_modules is required")
    require(all(isinstance(value, str) and value for value in target_modules), "invalid LoRA target module")
    stages = config.get("training_stages")
    require(isinstance(stages, list) and stage in stages, "training_stages must include the current stage")
    require(set(stages) <= STAGES, "training_stages contains an unknown stage")

    inputs = [
        resolve_path(config["foundation_path"], "foundation_path"),
        resolve_path(config["tokenizer_path"], "tokenizer_path"),
        resolve_path(config["dataset_root"], "dataset_root"),
    ]
    if parent:
        inputs.append(resolve_path(parent, "parent_adapter_path"))
    output = resolve_path(config["output_dir"], "output_dir")
    for source in inputs:
        require(output != source and output not in source.parents, "output cannot contain an immutable input")
        require(source not in output.parents, "output cannot be nested under an immutable input")
    return config


def load_training_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(config["dataset_root"])
    manifest_path = Path(config["dataset_manifest"])
    manifest = json.loads(manifest_path.read_text())
    records_path = (root / manifest["records"]).resolve()
    require(root.resolve() in records_path.parents, "records path escapes dataset root")
    require(f"sha256:{sha256_file(records_path)}" == manifest["records_digest"], "records digest mismatch")
    selected: list[dict[str, Any]] = []
    allowed_stages = set(config["training_stages"])
    for raw in records_path.read_text().splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        if record.get("split") in TRAINING_SPLITS and record.get("stage") in allowed_stages:
            selected.append(record)
    require(selected, "no training records match the selected stages")
    return selected


@dataclass
class SupervisedFineTuningDataset:
    records: list[dict[str, Any]]
    tokenizer: Any
    max_length: int

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        messages = self.records[index]["messages"]
        prompt = messages[:-1]
        full_ids = self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=False,
        )[: self.max_length]
        prompt_ids = self.tokenizer.apply_chat_template(
            prompt,
            tokenize=True,
            add_generation_prompt=True,
        )[: self.max_length]
        labels = list(full_ids)
        for offset in range(min(len(prompt_ids), len(labels))):
            labels[offset] = -100
        require(any(value != -100 for value in labels), f"record {self.records[index]['id']} has no trainable tokens")
        return {"input_ids": list(full_ids), "attention_mask": [1] * len(full_ids), "labels": labels}


class SupervisedDataCollator:
    def __init__(self, pad_token_id: int):
        self.pad_token_id = pad_token_id

    def __call__(self, features: list[dict[str, list[int]]]) -> dict[str, Any]:
        import torch

        length = max(len(item["input_ids"]) for item in features)
        batch: dict[str, list[list[int]]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for item in features:
            padding = length - len(item["input_ids"])
            batch["input_ids"].append(item["input_ids"] + ([self.pad_token_id] * padding))
            batch["attention_mask"].append(item["attention_mask"] + ([0] * padding))
            batch["labels"].append(item["labels"] + ([-100] * padding))
        return {key: torch.tensor(value, dtype=torch.long) for key, value in batch.items()}


def gpu_identity(torch: Any, rank: int) -> dict[str, Any]:
    device = torch.cuda.current_device()
    properties = torch.cuda.get_device_properties(device)
    return {
        "rank": rank,
        "cuda_device": device,
        "name": properties.name,
        "compute_capability": f"{properties.major}.{properties.minor}",
        "total_memory_bytes": properties.total_memory,
    }


def gather_rank_identities(torch: Any, world_size: int, rank: int) -> list[dict[str, Any]]:
    local = gpu_identity(torch, rank)
    if world_size == 1:
        return [local]
    import torch.distributed as distributed

    require(distributed.is_initialized(), "distributed process group is not initialized")
    identities: list[Any] = [None] * world_size
    distributed.all_gather_object(identities, local)
    return sorted(identities, key=lambda item: item["rank"])


def rank_zero_value(torch: Any, world_size: int, rank: int, local_rank: int, factory: Any) -> Any:
    if world_size == 1:
        return factory()
    import torch.distributed as distributed

    require(distributed.is_initialized(), "distributed process group is not initialized")
    values = [factory() if rank == 0 else None]
    distributed.broadcast_object_list(values, src=0, device=torch.device("cuda", local_rank))
    return values[0]


def train(config_path: Path) -> None:
    config = validate_training_config(load_config(config_path))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments, set_seed

    require(torch.cuda.is_available(), "CUDA is unavailable")
    world_size = int(os.environ.get("WORLD_SIZE", "1"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    rank = int(os.environ.get("RANK", "0"))
    expected_gpu_count = int(config["training"]["expected_gpu_count"])
    require(world_size == expected_gpu_count, f"expected {expected_gpu_count} ranks, observed {world_size}")
    torch.cuda.set_device(local_rank)
    if world_size > 1 and not torch.distributed.is_initialized():
        torch.distributed.init_process_group(
            backend="nccl",
            device_id=torch.device("cuda", local_rank),
        )
    set_seed(int(config["seed"]))

    foundation = Path(config["foundation_path"])
    tokenizer_path = Path(config["tokenizer_path"])
    dataset_manifest = Path(config["dataset_manifest"])
    output = Path(config["output_dir"])
    require(foundation.is_dir(), "foundation path is missing")
    require(tokenizer_path.is_dir(), "tokenizer path is missing")
    require(dataset_manifest.is_file(), "dataset manifest is missing")
    foundation_digest = rank_zero_value(
        torch,
        world_size,
        rank,
        local_rank,
        lambda: f"sha256:{sha256_tree(foundation)}",
    )
    require(foundation_digest == config["foundation_digest"], "foundation digest mismatch")
    require(
        f"sha256:{sha256_file(dataset_manifest)}" == config["dataset_manifest_digest"],
        "dataset manifest digest mismatch",
    )

    parent_path = Path(config["parent_adapter_path"]) if config.get("parent_adapter_path") else None
    parent_before = rank_zero_value(
        torch,
        world_size,
        rank,
        local_rank,
        lambda: sha256_tree(parent_path) if parent_path else None,
    )
    if parent_path:
        require(f"sha256:{parent_before}" == config["parent_adapter_digest"], "parent adapter digest mismatch")

    records = load_training_records(config)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        require(tokenizer.eos_token_id is not None, "tokenizer has no pad or EOS token")
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        foundation,
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        device_map={"": local_rank},
    )
    model.config.use_cache = False
    training = config["training"]
    if parent_path:
        model = PeftModel.from_pretrained(model, parent_path, is_trainable=True)
    else:
        model = get_peft_model(
            model,
            LoraConfig(
                task_type="CAUSAL_LM",
                r=int(training["lora_rank"]),
                lora_alpha=int(training["lora_alpha"]),
                lora_dropout=float(training["lora_dropout"]),
                target_modules=list(training["target_modules"]),
                bias="none",
            ),
        )
    model.enable_input_require_grads()
    dataset = SupervisedFineTuningDataset(records, tokenizer, int(training["sequence_length"]))
    arguments = TrainingArguments(
        output_dir=str(output / "checkpoints"),
        overwrite_output_dir=False,
        max_steps=int(training["max_steps"]),
        per_device_train_batch_size=int(training["per_device_batch_size"]),
        gradient_accumulation_steps=int(training["gradient_accumulation_steps"]),
        learning_rate=float(training["learning_rate"]),
        weight_decay=float(training["weight_decay"]),
        warmup_ratio=float(training["warmup_ratio"]),
        max_grad_norm=float(training["max_grad_norm"]),
        lr_scheduler_type="cosine",
        optim="adamw_torch_fused",
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        save_strategy="steps",
        save_steps=int(training["save_steps"]),
        save_total_limit=int(training.get("save_total_limit", 3)),
        logging_steps=int(training.get("logging_steps", 5)),
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=int(training.get("dataloader_num_workers", 2)),
        ddp_find_unused_parameters=False if world_size > 1 else None,
        seed=int(config["seed"]),
        data_seed=int(config["seed"]),
    )
    trainer = Trainer(
        model=model,
        args=arguments,
        train_dataset=dataset,
        data_collator=SupervisedDataCollator(tokenizer.pad_token_id),
    )
    identities = gather_rank_identities(torch, world_size, rank)
    result = trainer.train(resume_from_checkpoint=config.get("resume_checkpoint") or None)
    trainer.accelerator.wait_for_everyone()

    if trainer.is_world_process_zero():
        adapter_output = output / "adapter"
        tokenizer_output = output / "tokenizer"
        model.save_pretrained(adapter_output, safe_serialization=True)
        tokenizer.save_pretrained(tokenizer_output)
    trainer.accelerator.wait_for_everyone()
    parent_after = rank_zero_value(
        torch,
        world_size,
        rank,
        local_rank,
        lambda: sha256_tree(parent_path) if parent_path else None,
    )
    if trainer.is_world_process_zero():
        adapter_output = output / "adapter"
        require(parent_before == parent_after, "parent adapter changed during child training")
        adapter_digest = sha256_tree(adapter_output)
        write_json(
            output / "training-result.json",
            {
                "schema_version": "1.0.0",
                "status": "CANDIDATE",
                "stage": config["stage"],
                "foundation_digest": config["foundation_digest"],
                "dataset_manifest_digest": config["dataset_manifest_digest"],
                "parent_adapter_digest": config.get("parent_adapter_digest"),
                "parent_unchanged": parent_before == parent_after,
                "adapter_digest": f"sha256:{adapter_digest}",
                "world_size": world_size,
                "rank_identities": identities,
                "effective_global_batch": (
                    int(training["per_device_batch_size"])
                    * world_size
                    * int(training["gradient_accumulation_steps"])
                ),
                "train_records": len(records),
                "global_step": result.global_step,
                "training_loss": result.training_loss,
                "config_digest": f"sha256:{hashlib.sha256(canonical_json(config)).hexdigest()}",
            },
        )
    trainer.accelerator.wait_for_everyone()
    if torch.distributed.is_initialized():
        torch.distributed.destroy_process_group()


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline multi-GPU text-adapter LoRA trainer")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        train(Path(args.config))
    except TrainingContractError as error:
        raise SystemExit(f"training contract error: {error}") from error


if __name__ == "__main__":
    main()


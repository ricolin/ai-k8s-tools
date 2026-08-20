from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


TRAINER_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/trainer.py"
SPEC = importlib.util.spec_from_file_location("security_trainer", TRAINER_PATH)
assert SPEC is not None and SPEC.loader is not None
trainer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trainer
SPEC.loader.exec_module(trainer)


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def config(tmp_path: Path, stage: str = "A") -> dict:
    foundation = tmp_path / "foundation"
    tokenizer = tmp_path / "tokenizer"
    dataset = tmp_path / "dataset"
    for path in (foundation, tokenizer, dataset):
        path.mkdir(exist_ok=True)
    value = {
        "schema_version": "1.0.0",
        "stage": stage,
        "foundation_path": str(foundation),
        "foundation_digest": digest("a"),
        "tokenizer_path": str(tokenizer),
        "dataset_root": str(dataset),
        "dataset_manifest": str(dataset / "manifest.json"),
        "dataset_manifest_digest": digest("b"),
        "parent_adapter_path": None,
        "parent_adapter_digest": None,
        "output_dir": str(tmp_path / "output"),
        "training_stages": [stage],
        "seed": 7,
        "training": {
            "expected_gpu_count": 8,
            "max_steps": 100,
            "save_steps": 10,
            "sequence_length": 4096,
            "per_device_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "learning_rate": 0.0001,
            "weight_decay": 0.01,
            "warmup_ratio": 0.05,
            "max_grad_norm": 1.0,
            "lora_rank": 64,
            "lora_alpha": 128,
            "lora_dropout": 0.05,
            "target_modules": ["q_proj", "v_proj"],
        },
    }
    if stage in {"B", "C"}:
        parent = tmp_path / "parent"
        parent.mkdir()
        value["parent_adapter_path"] = str(parent)
        value["parent_adapter_digest"] = digest("c")
    return value


def test_stage_a_has_no_parent_and_stage_b_requires_one(tmp_path: Path) -> None:
    assert trainer.validate_training_config(config(tmp_path, "A"))["stage"] == "A"
    value = config(tmp_path, "B")
    assert trainer.validate_training_config(value)["parent_adapter_path"].endswith("parent")
    value["parent_adapter_path"] = None
    with pytest.raises(trainer.TrainingContractError, match="absolute path"):
        trainer.validate_training_config(value)


def test_output_cannot_overlap_immutable_inputs(tmp_path: Path) -> None:
    value = config(tmp_path)
    value["output_dir"] = value["foundation_path"] + "/output"
    with pytest.raises(trainer.TrainingContractError, match="nested under"):
        trainer.validate_training_config(value)


def test_dataset_masks_prompt_and_keeps_assistant_tokens() -> None:
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            assert tokenize is True
            if add_generation_prompt:
                return [1, 2, 3]
            return [1, 2, 3, 4, 5]

    dataset = trainer.SecuritySFTDataset(
        [{"id": "fixture", "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]}],
        Tokenizer(),
        16,
    )
    record = dataset[0]
    assert record["labels"] == [-100, -100, -100, 4, 5]


def test_effective_batch_contract_is_explicit(tmp_path: Path) -> None:
    value = trainer.validate_training_config(config(tmp_path))
    training = value["training"]
    effective = (
        training["per_device_batch_size"]
        * training["expected_gpu_count"]
        * training["gradient_accumulation_steps"]
    )
    assert effective == 8

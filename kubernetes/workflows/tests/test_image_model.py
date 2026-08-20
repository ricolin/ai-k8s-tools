from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ai_build_tools_k8s.image_model import (
    ContractError,
    create_release_manifest,
    render_image_job,
    validate_comparison_prompts,
)


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def test_comparison_prompts_are_fixed_and_1024(tmp_path: Path) -> None:
    prompts = [
        {
            "id": f"scene-{index}",
            "prompt": "the same watercolor architecture request",
            "negative_prompt": "text, watermark",
            "seed": 100 + index,
            "width": 1024,
            "height": 1024,
            "steps": 28,
            "guidance": 4.5,
        }
        for index in range(3)
    ]
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(prompts))
    assert validate_comparison_prompts(path)["prompt_count"] == 3
    prompts[0]["width"] = 512
    path.write_text(json.dumps(prompts))
    with pytest.raises(ContractError, match="1024x1024"):
        validate_comparison_prompts(path)


def test_image_jobs_have_explicit_gpu_scope_and_offline_inputs() -> None:
    image = "registry.example/image@" + digest("a")
    training = render_image_job(
        "image-a",
        "ai-workflows",
        image,
        "workspace",
        "/workspace/config/A.json",
        8,
        "train",
        "accelerator",
        "h200",
    )
    generation = render_image_job(
        "image-a-gallery",
        "ai-workflows",
        image,
        "workspace",
        "/workspace/config/generate-A.json",
        1,
        "generate",
        "accelerator",
        "h200",
    )
    train_container = training["spec"]["template"]["spec"]["containers"][0]
    generate_container = generation["spec"]["template"]["spec"]["containers"][0]
    assert train_container["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert generate_container["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert training["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert {item["name"]: item["value"] for item in train_container["env"]}["HF_HUB_OFFLINE"] == "1"


def test_image_release_records_ordered_composition() -> None:
    release = create_release_manifest(
        "release-b-watercolor-detail",
        digest("a"),
        [
            {"name": "watercolor", "digest": digest("b"), "scale": 1.0},
            {"name": "detail", "digest": digest("c"), "scale": 0.8},
        ],
        digest("d"),
        digest("e"),
        "AI_BLIND_REVIEWED",
    )
    assert [adapter["name"] for adapter in release["adapters"]] == ["watercolor", "detail"]


TRAIN_STAGE = Path(__file__).parents[3] / "kubernetes-CUDA/image/train_stage.py"
SPEC = importlib.util.spec_from_file_location("image_train_stage", TRAIN_STAGE)
assert SPEC is not None and SPEC.loader is not None
image_train = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_train
SPEC.loader.exec_module(image_train)


def image_training_config(tmp_path: Path, stage: str) -> dict:
    base = tmp_path / "base"
    dataset = tmp_path / "dataset"
    base.mkdir(exist_ok=True)
    dataset.mkdir(exist_ok=True)
    value = {
        "schema_version": "1.0.0",
        "stage": stage,
        "base_path": str(base),
        "base_digest": digest("a"),
        "prepared_dataset_path": str(dataset),
        "prepared_dataset_digest": digest("b"),
        "parent_adapter_path": None,
        "parent_adapter_digest": None,
        "output_dir": str(tmp_path / "output"),
        "fallback_prompt": "watercolor",
        "seed": 1,
        "training": {
            "gpu_count": 8,
            "max_steps": 100,
            "checkpoint_steps": 10,
            "resolution": 1024,
            "rank": 64,
            "batch_size": 1,
            "gradient_accumulation": 1,
            "learning_rate": 0.0001,
            "parent_scale": 1.0,
        },
    }
    if stage == "B-detail":
        parent = tmp_path / "parent"
        parent.mkdir()
        value["parent_adapter_path"] = str(parent)
        value["parent_adapter_digest"] = digest("c")
    return value


def test_b_detail_requires_immutable_parent(tmp_path: Path) -> None:
    assert image_train.validate_config(image_training_config(tmp_path, "B-detail"))["stage"] == "B-detail"
    value = image_training_config(tmp_path, "A")
    value["parent_adapter_path"] = "/unexpected"
    with pytest.raises(SystemExit, match="cannot have a parent"):
        image_train.validate_config(value)

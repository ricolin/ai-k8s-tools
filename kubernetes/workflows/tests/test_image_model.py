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
    assert {item["name"]: item for item in training["spec"]["template"]["spec"]["volumes"]}["dshm"] == {
        "name": "dshm",
        "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"},
    }
    assert {item["mountPath"] for item in train_container["volumeMounts"]} == {"/workspace", "/dev/shm"}


def test_node_local_image_requires_never_and_records_runtime_id() -> None:
    job = render_image_job(
        "image-a",
        "ai-workflows",
        "ai-k8s-tools.local/image-workflow:260820-v3",
        "workspace",
        "/workspace/config/A.json",
        8,
        "train",
        "accelerator",
        "h200",
        "Never",
        digest("f"),
    )
    assert job["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] == "Never"
    assert job["metadata"]["annotations"]["ai-build-tools.ricolin.dev/node-local-image-id"] == digest("f")
    with pytest.raises(ContractError, match="node_local_image_id"):
        render_image_job(
            "image-a",
            "ai-workflows",
            "ai-k8s-tools.local/image-workflow:260820-v3",
            "workspace",
            "/workspace/config/A.json",
            8,
            "train",
            "accelerator",
            "h200",
            "Never",
        )


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


def test_impressionist_release_records_watercolor_parent_first() -> None:
    release = create_release_manifest(
        "release-b-watercolor-impressionism",
        digest("a"),
        [
            {"name": "watercolor", "digest": digest("b"), "scale": 1.0},
            {"name": "impressionism", "digest": digest("c"), "scale": 0.8},
        ],
        digest("d"),
        digest("e"),
        "AI_BLIND_REVIEWED",
    )
    assert [adapter["name"] for adapter in release["adapters"]] == ["watercolor", "impressionism"]


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
    if stage in {"B-detail", "B-impressionism"}:
        parent = tmp_path / "parent"
        parent.mkdir(exist_ok=True)
        value["parent_adapter_path"] = str(parent)
        value["parent_adapter_digest"] = digest("c")
    return value


def test_b_detail_requires_immutable_parent(tmp_path: Path) -> None:
    assert image_train.validate_config(image_training_config(tmp_path, "B-detail"))["stage"] == "B-detail"
    value = image_training_config(tmp_path, "A")
    value["parent_adapter_path"] = "/unexpected"
    with pytest.raises(SystemExit, match="cannot have a parent"):
        image_train.validate_config(value)

    impressionism = image_training_config(tmp_path, "B-impressionism")
    assert image_train.validate_config(impressionism)["stage"] == "B-impressionism"


def test_a_stage_launcher_writes_candidate_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = image_training_config(tmp_path, "A")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(image_train, "sha256_tree", lambda _path: "a" * 64)
    monkeypatch.setattr(image_train, "verify_prepared_dataset", lambda _path, _digest: {"status": "PASS"})

    def fake_run(command: list[str], check: bool) -> None:
        assert check is True
        assert command[:3] == [sys.executable, "-m", "accelerate.commands.launch"]
        assert command[command.index("--report_to") + 1] == "tensorboard"
        trainer_output = Path(command[command.index("--output_dir") + 1])
        trainer_output.mkdir(parents=True)
        (trainer_output / "pytorch_lora_weights.safetensors").write_bytes(b"adapter")

    monkeypatch.setattr(image_train.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["train_stage.py", "--config", str(config_path)])
    image_train.main()
    result = json.loads((tmp_path / "output" / "training-result.json").read_text())
    assert result["status"] == "CANDIDATE"
    assert result["stage"] == "A"
    assert result["gpu_count"] == 8


def test_demo_dataset_is_deterministic_and_manifest_ready(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    demo_dataset = Path(__file__).parents[3] / "kubernetes-CUDA/image/generate_demo_dataset.py"
    demo_spec = importlib.util.spec_from_file_location("image_demo_dataset", demo_dataset)
    assert demo_spec is not None and demo_spec.loader is not None
    image_demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[demo_spec.name] = image_demo
    demo_spec.loader.exec_module(image_demo)
    first = image_demo.write_stage(tmp_path / "first", "A", 1, 260820)[0]
    second = image_demo.write_stage(tmp_path / "second", "A", 1, 260820)[0]
    assert first["sha256"] == second["sha256"]
    assert first["license"] == "CC0-1.0"
    assert first["permission_confirmed"] is True
    assert first["caption"].startswith("abt_watercolor,")


def test_demo_impressionism_is_distinct_and_deterministic(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    demo_dataset = Path(__file__).parents[3] / "kubernetes-CUDA/image/generate_demo_dataset.py"
    demo_spec = importlib.util.spec_from_file_location("image_demo_dataset_impressionism", demo_dataset)
    assert demo_spec is not None and demo_spec.loader is not None
    image_demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[demo_spec.name] = image_demo
    demo_spec.loader.exec_module(image_demo)
    watercolor = image_demo.write_stage(tmp_path / "watercolor", "A", 1, 260820)[0]
    first = image_demo.write_stage(tmp_path / "first", "B-impressionism", 1, 260820)[0]
    second = image_demo.write_stage(tmp_path / "second", "B-impressionism", 1, 260820)[0]
    assert first["sha256"] == second["sha256"]
    assert first["sha256"] != watercolor["sha256"]
    assert "abt_impressionism" in first["caption"]

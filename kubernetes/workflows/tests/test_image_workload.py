from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_build_tools_k8s.image_model import ContractError
from ai_build_tools_k8s.image_workload import (
    hydrate_generation_adapters,
    hydrate_training_parent,
    stage_evidence_dir,
    terminal_state,
    validate_dependencies,
    validate_training_result,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def test_terminal_state_and_safe_evidence_stage(tmp_path: Path) -> None:
    assert terminal_state({"status": {"conditions": []}}) is None
    assert terminal_state(
        {"status": {"conditions": [{"type": "Succeeded", "status": "True"}]}}
    ) == "COMPLETE"
    assert stage_evidence_dir(tmp_path, "gallery-a") == tmp_path / "gallery-a"
    with pytest.raises(ContractError, match="lowercase name"):
        stage_evidence_dir(tmp_path, "../gallery-a")


def test_release_b_hydrates_the_parent_tree_digest(tmp_path: Path) -> None:
    config_path = tmp_path / "b.json"
    result_path = tmp_path / "a-result.json"
    config_path.write_text(
        json.dumps(
            {
                "stage": "B-impressionism",
                "parent_adapter_path": "/workspace/runs/A/adapter",
                "parent_adapter_digest": None,
            }
        )
    )
    result_path.write_text(
        json.dumps(
            {
                "state": "COMPLETE",
                "stage": "A",
                "adapter_tree_digest": digest("a"),
            }
        )
    )

    result = hydrate_training_parent(config_path, result_path)

    assert result["parent_adapter_digest"] == digest("a")
    assert json.loads(config_path.read_text())["parent_adapter_digest"] == digest("a")


def test_gallery_hydrates_ordered_adapter_file_digests(tmp_path: Path) -> None:
    config_path = tmp_path / "gallery.json"
    result_a = tmp_path / "a.json"
    result_b = tmp_path / "b.json"
    config_path.write_text(
        json.dumps(
            {
                "adapters": [
                    {"name": "watercolor", "digest": None},
                    {"name": "impressionism", "digest": None},
                ]
            }
        )
    )
    result_a.write_text(json.dumps({"state": "COMPLETE", "adapter_digest": digest("a")}))
    result_b.write_text(json.dumps({"state": "COMPLETE", "adapter_digest": digest("b")}))

    result = hydrate_generation_adapters(config_path, [result_a, result_b])

    assert [adapter["digest"] for adapter in result["adapters"]] == [digest("a"), digest("b")]


def test_dependency_must_be_complete(tmp_path: Path) -> None:
    path = tmp_path / "dependency.json"
    path.write_text(json.dumps({"state": "FAILED"}))
    with pytest.raises(ContractError, match="not complete"):
        validate_dependencies([path])


def test_training_result_enforces_stage_gpu_reservation_and_digests(tmp_path: Path) -> None:
    path = tmp_path / "training-result.json"
    path.write_text(
        json.dumps(
            {
                "stage": "A",
                "gpu_count": 7,
                "adapter_digest": digest("a"),
                "adapter_tree_digest": digest("b"),
            }
        )
    )
    assert validate_training_result(path, "A", 7)["gpu_count"] == 7
    with pytest.raises(ContractError, match="GPU count mismatch"):
        validate_training_result(path, "A", 8)

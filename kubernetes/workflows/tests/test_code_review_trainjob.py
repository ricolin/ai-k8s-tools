from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_build_tools_k8s.code_review_model import ContractError
from ai_build_tools_k8s.code_review_trainjob import (
    hydrate_parent_digest,
    load_training_result,
    stage_evidence_dir,
    terminal_state,
)


def test_trainjob_terminal_state() -> None:
    assert terminal_state({"status": {"conditions": []}}) is None
    assert terminal_state(
        {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    ) == "COMPLETE"
    assert terminal_state(
        {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
    ) == "FAILED"


def test_stage_evidence_dir_joins_only_a_lowercase_stage(tmp_path: Path) -> None:
    assert stage_evidence_dir(tmp_path, "release-a") == tmp_path / "release-a"
    with pytest.raises(ContractError, match="lowercase name"):
        stage_evidence_dir(tmp_path, "../release-a")


def test_parent_digest_is_hydrated_from_the_previous_stage(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    config_path = tmp_path / "release-b.json"
    parent_path = tmp_path / "release-a-result.json"
    config_path.write_text(
        json.dumps(
            {
                "stage": "B",
                "parent_adapter_path": "/workspace/release-a/adapter",
                "parent_adapter_digest": None,
            }
        )
    )
    parent_path.write_text(
        json.dumps({"state": "COMPLETE", "stage": "A", "adapter_digest": digest})
    )

    observed = hydrate_parent_digest(config_path, parent_path)

    assert observed["parent_adapter_digest"] == digest
    assert json.loads(config_path.read_text())["parent_adapter_digest"] == digest


def test_parent_digest_rejects_a_different_preconfigured_identity(tmp_path: Path) -> None:
    config_path = tmp_path / "release-b.json"
    parent_path = tmp_path / "release-a-result.json"
    config_path.write_text(
        json.dumps(
            {
                "stage": "B",
                "parent_adapter_path": "/workspace/release-a/adapter",
                "parent_adapter_digest": "sha256:" + "b" * 64,
            }
        )
    )
    parent_path.write_text(
        json.dumps(
            {
                "state": "COMPLETE",
                "stage": "A",
                "adapter_digest": "sha256:" + "a" * 64,
            }
        )
    )

    with pytest.raises(ContractError, match="different parent adapter digest"):
        hydrate_parent_digest(config_path, parent_path)


def test_training_result_requires_stage_eight_gpus_and_adapter_digest(tmp_path: Path) -> None:
    result_path = tmp_path / "training-result.json"
    result_path.write_text(
        json.dumps(
            {
                "stage": "C",
                "world_size": 8,
                "adapter_digest": "sha256:" + "c" * 64,
            }
        )
    )
    assert load_training_result(result_path, "C", 8)["world_size"] == 8

    with pytest.raises(ContractError, match="stage mismatch"):
        load_training_result(result_path, "B", 8)

    with pytest.raises(ContractError, match="world size mismatch"):
        load_training_result(result_path, "C", 4)

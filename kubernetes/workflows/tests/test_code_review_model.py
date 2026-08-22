from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_build_tools_k8s.code_review_model import (
    ContractError,
    record_digest,
    render_comparison_job,
    render_release_job,
    render_node_local_serving,
    render_serving,
    render_training_job,
    validate_dataset,
    validate_dataset_record,
    validate_release,
)


def digest(character: str = "a") -> str:
    return "sha256:" + (character * 64)


def release() -> dict:
    return {
        "schema_version": "1.0.0",
        "stage": "C",
        "validation_level": "AUTOMATED_ACCEPTED",
        "foundation_digest": digest("a"),
        "adapter_digest": digest("b"),
        "tokenizer_digest": digest("c"),
        "chat_template_digest": digest("d"),
        "review_schema_digest": digest("e"),
        "agent_plan_schema_digest": digest("f"),
        "policy_profile_digest": digest("1"),
        "serving_model_name": "code-reviewer-c",
        "lora_rank": 16,
        "supported_languages": ["bash", "go", "python", "rust", "yaml"],
        "supported_target_types": ["agent-plan", "pull-request", "repository", "single-file"],
    }


def record(language: str, identifier: str) -> dict:
    value = {
        "id": identifier,
        "stage": "A",
        "split": "train",
        "source": "fixture",
        "license": "CC0-1.0",
        "permission_confirmed": True,
        "target_type": "single-file",
        "languages": [language],
        "messages": [
            {"role": "user", "content": "review"},
            {"role": "assistant", "content": "result"},
        ],
    }
    value["record_digest"] = record_digest(value)
    return value


def test_release_contract() -> None:
    assert validate_release(release()) == release()
    invalid = release()
    invalid["supported_languages"].remove("rust")
    with pytest.raises(ContractError, match="languages are incomplete"):
        validate_release(invalid)


def test_dataset_covers_every_language(tmp_path: Path) -> None:
    records = [record(language, f"record-{index}") for index, language in enumerate(("bash", "go", "python", "rust", "yaml"))]
    records_path = tmp_path / "records.jsonl"
    records_path.write_text("".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in records))
    manifest = {
        "schema_version": "1.0.0",
        "license_review_complete": True,
        "records": "records.jsonl",
        "records_digest": "sha256:" + hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "record_count": 5,
        "stage_counts": {"A": 5, "B": 0, "C": 0},
        "split_counts": {"adversarial": 0, "hidden": 0, "train": 5, "validation": 0},
        "language_counts": {"bash": 1, "go": 1, "python": 1, "rust": 1, "yaml": 1},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    assert validate_dataset(manifest_path, tmp_path)["status"] == "PASS"
    records[0]["record_digest"] = digest("0")
    with pytest.raises(ContractError, match="record digest mismatch"):
        validate_dataset_record(records[0])


def test_training_job_uses_code_review_entrypoint() -> None:
    value = render_training_job(
        "review-a",
        "ai-workflows",
        "reviewer:v1",
        "workspace",
        "/workspace/configs/a.json",
        8,
        "accelerator",
        "h200",
        "Never",
        digest("a"),
    )
    container = value["spec"]["template"]["spec"]["containers"][0]
    assert "/opt/ai-code-review/trainer.py" in container["args"]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert container["securityContext"]["runAsUser"] == 65532
    assert value["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    assert "tolerations" not in value["spec"]["template"]["spec"]


def test_training_and_serving_can_tolerate_control_plane_taints() -> None:
    training = render_training_job(
        "review-a",
        "ai-workflows",
        "reviewer:v1",
        "workspace",
        "/workspace/configs/a.json",
        8,
        "accelerator",
        "h200",
        "Never",
        digest("a"),
        True,
    )
    serving = render_serving(
        release(),
        "code-reviewer-c",
        "ai-workflows",
        "registry.example/vllm@" + digest("2"),
        "registry.example/verifier@" + digest("3"),
        "workspace",
        1,
        "accelerator",
        "h200",
        True,
    )
    expected = [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
        {
            "key": "node-role.kubernetes.io/master",
            "operator": "Exists",
            "effect": "NoSchedule",
        },
    ]
    assert training["spec"]["template"]["spec"]["tolerations"] == expected
    assert serving["spec"]["predictor"]["tolerations"] == expected


def test_node_local_serving_uses_the_verified_adapter_runtime() -> None:
    value = render_node_local_serving(
        release(),
        "code-reviewer-c",
        "ai-workflows",
        "reviewer:v1",
        "workspace",
        "/workspace/foundation",
        "/workspace/adapter",
        "accelerator",
        "h200",
        "Never",
        digest("9"),
        True,
    )
    predictor = value["spec"]["predictor"]
    container = predictor["containers"][0]

    assert container["command"] == [
        "/opt/ai-venv/bin/python",
        "/opt/ai-code-review/serve_reviewer.py",
    ]
    assert container["args"][container["args"].index("--foundation-digest") + 1] == digest("a")
    assert container["args"][container["args"].index("--adapter-digest") + 1] == digest("b")
    assert container["args"][container["args"].index("--response-prefix") + 1] == "{"
    assert container["imagePullPolicy"] == "Never"
    assert predictor["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert predictor["tolerations"][0]["key"] == "node-role.kubernetes.io/control-plane"
    assert value["metadata"]["annotations"]["ai-k8s-tools.ricolin.dev/node-local-image-id"] == digest("9")


def test_comparison_job_uses_one_gpu_and_restricted_identity() -> None:
    value = render_comparison_job(
        "compare",
        "ai-workflows",
        "reviewer:v1",
        "workspace",
        "/workspace/configs/compare.json",
        "accelerator",
        "h200",
        "Never",
        digest("8"),
        True,
    )
    pod = value["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert container["command"][-1] == "/opt/ai-code-review/evaluate_reviewer.py"
    assert container["resources"]["requests"]["nvidia.com/gpu"] == 1
    assert pod["securityContext"]["runAsUser"] == 65532
    assert value["spec"]["activeDeadlineSeconds"] == 7200


def test_release_job_verifies_mounted_artifacts_without_a_gpu() -> None:
    value = render_release_job(
        "release-c",
        "ai-workflows",
        "reviewer:v1",
        "workspace",
        digest("a"),
        "/workspace/foundation",
        "/workspace/adapter",
        "/workspace/tokenizer",
        "/workspace/release/chat-template.jinja",
        "/workspace/release/review.schema.json",
        "/workspace/release/agent-plan.schema.json",
        "/workspace/release/policy-profile.json",
        "/workspace/release/code-review-release.json",
        "/workspace/release/mounted-verification.json",
        16,
        "accelerator",
        "h200",
        "Never",
        digest("8"),
        True,
    )
    pod = value["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert container["command"][-1] == "ai_build_tools_k8s.code_review_model"
    assert container["args"][0] == "create-and-verify-release"
    assert "nvidia.com/gpu" not in container["resources"]["requests"]
    assert pod["securityContext"]["runAsUser"] == 65532
    assert pod["tolerations"][0]["key"] == "node-role.kubernetes.io/control-plane"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_build_tools_k8s.security_model import (
    ContractError,
    _record_digest,
    create_adviser_release,
    render_adviser_inference_service,
    render_security_training_job,
    validate_adviser_release,
    validate_dataset,
    verify_mounted_release,
)
from ai_build_tools_k8s.workflow import canonical_json, sha256_file, write_json


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def dataset_record(identifier: str = "a-1") -> dict:
    value = {
        "id": identifier,
        "stage": "A",
        "split": "train",
        "source": "synthetic fixture",
        "license": "CC0-1.0",
        "permission_confirmed": True,
        "target_type": "general-defense",
        "messages": [
            {"role": "user", "content": "Review this synthetic configuration."},
            {"role": "assistant", "content": "The supplied evidence shows one finding."},
        ],
        "evidence_ids": ["fixture-1"],
        "allowed_operations": ["analysis"],
        "forbidden_operations": ["source-write"],
    }
    value["record_digest"] = _record_digest(value)
    return value


def create_dataset(tmp_path: Path) -> tuple[Path, Path]:
    records = tmp_path / "records.jsonl"
    records.write_bytes(canonical_json(dataset_record()) + b"\n")
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "schema_version": "1.0.0",
            "dataset_name": "fixture",
            "license_review_complete": True,
            "records": "records.jsonl",
            "records_digest": f"sha256:{sha256_file(records)}",
            "record_count": 1,
            "stage_counts": {"A": 1, "B": 0, "C": 0},
            "split_counts": {"adversarial": 0, "hidden": 0, "train": 1, "validation": 0},
        },
    )
    return manifest, records


def create_release_tree(tmp_path: Path) -> tuple[dict, dict[str, Path]]:
    paths = {
        "foundation": tmp_path / "foundation",
        "adapter": tmp_path / "adapter",
        "tokenizer": tmp_path / "tokenizer",
        "chat_template": tmp_path / "chat-template.jinja",
        "verification_plan_schema": tmp_path / "verification-plan.schema.json",
        "finding_schema": tmp_path / "finding.schema.json",
        "policy_profile": tmp_path / "policy-profile.json",
    }
    for name in ("foundation", "adapter", "tokenizer"):
        paths[name].mkdir()
        (paths[name] / f"{name}.bin").write_text(name)
    for name in ("chat_template", "verification_plan_schema", "finding_schema", "policy_profile"):
        paths[name].write_text(name)
    foundation_digest = digest("a")
    release = create_adviser_release(
        foundation_digest,
        paths["adapter"],
        paths["tokenizer"],
        paths["chat_template"],
        paths["verification_plan_schema"],
        paths["finding_schema"],
        paths["policy_profile"],
        64,
    )
    # The mounted verifier hashes the physical foundation. Replace the fixture
    # lock with that observed digest to test the same identity contract.
    from ai_build_tools_k8s.workflow import sha256_tree

    release["foundation_digest"] = f"sha256:{sha256_tree(paths['foundation'])}"
    return release, paths


def test_dataset_contract_and_record_digest(tmp_path: Path) -> None:
    manifest, _ = create_dataset(tmp_path)
    report = validate_dataset(manifest, tmp_path)
    assert report["status"] == "PASS"
    assert report["stage_counts"] == {"A": 1, "B": 0, "C": 0}


def test_dataset_rejects_unlicensed_or_modified_records(tmp_path: Path) -> None:
    manifest, records = create_dataset(tmp_path)
    value = dataset_record()
    value["messages"][-1]["content"] = "changed without recomputing identity"
    records.write_bytes(canonical_json(value) + b"\n")
    metadata = json.loads(manifest.read_text())
    metadata["records_digest"] = f"sha256:{sha256_file(records)}"
    write_json(manifest, metadata)
    with pytest.raises(ContractError, match="record digest mismatch"):
        validate_dataset(manifest, tmp_path)


def test_training_job_is_eight_gpu_offline_and_non_privileged() -> None:
    image = "registry.example/trainer@" + digest("b")
    job = render_security_training_job(
        "adviser-a",
        "ai-workflows",
        image,
        "model-workspace",
        "/workspace/configs/a.json",
        8,
        "accelerator",
        "h200",
    )
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert "--nproc-per-node=8" in container["args"]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == 8
    assert {item["name"]: item["value"] for item in container["env"]}["HF_HUB_OFFLINE"] == "1"
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert {mount["name"]: mount["mountPath"] for mount in container["volumeMounts"]}["dshm"] == "/dev/shm"
    assert next(volume for volume in pod["volumes"] if volume["name"] == "dshm")["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "32Gi",
    }


def test_training_job_rejects_mutable_image() -> None:
    with pytest.raises(ContractError, match="digest-pinned"):
        render_security_training_job(
            "adviser-a", "ai-workflows", "registry/trainer:latest", "pvc", "/workspace/a.json", 8, "", ""
        )


def test_training_job_accepts_guarded_node_local_image() -> None:
    image_id = digest("e")
    job = render_security_training_job(
        "adviser-a",
        "ai-workflows",
        "ai-k8s-tools.local/security-trainer:validation-260820",
        "model-workspace",
        "/workspace/configs/a.json",
        8,
        "accelerator",
        "h200",
        "Never",
        image_id,
    )
    assert job["metadata"]["annotations"] == {
        "ai-build-tools.ricolin.dev/node-local-image-id": image_id,
    }
    assert job["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] == "Never"


def test_node_local_training_image_requires_runtime_identity() -> None:
    with pytest.raises(ContractError, match="node_local_image_id"):
        render_security_training_job(
            "adviser-a",
            "ai-workflows",
            "ai-k8s-tools.local/security-trainer:validation-260820",
            "pvc",
            "/workspace/a.json",
            8,
            "",
            "",
            "Never",
        )


def test_release_and_kserve_identity_contract(tmp_path: Path) -> None:
    release, paths = create_release_tree(tmp_path)
    validate_adviser_release(release)
    service = render_adviser_inference_service(
        release,
        "security-adviser-c",
        "ai-workflows",
        "registry/vllm@" + digest("c"),
        "registry/verifier@" + digest("d"),
        "models",
        1,
        "accelerator",
        "h200",
    )
    predictor = service["spec"]["predictor"]
    assert predictor["automountServiceAccountToken"] is False
    assert predictor["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert "security-adviser-c=/models/adapter" in predictor["containers"][0]["args"]
    assert predictor["containers"][0]["args"][-2:] == ["--tensor-parallel-size", "1"]
    rank_index = predictor["containers"][0]["args"].index("--max-lora-rank")
    assert predictor["containers"][0]["args"][rank_index + 1] == "64"
    assert predictor["initContainers"][0]["command"] == ["ai-security-model"]

    release_path = tmp_path / "advisor-release.json"
    write_json(release_path, release)
    report = verify_mounted_release(
        release_path,
        paths["foundation"],
        paths["adapter"],
        paths["tokenizer"],
        paths["chat_template"],
        paths["verification_plan_schema"],
        paths["finding_schema"],
        paths["policy_profile"],
    )
    assert report["status"] == "PASS"


def test_release_rejects_non_blind_reviewed_state(tmp_path: Path) -> None:
    release, _ = create_release_tree(tmp_path)
    release["validation_level"] = "AUTOMATED_ACCEPTED"
    with pytest.raises(ContractError, match="not AI blind reviewed"):
        validate_adviser_release(release)

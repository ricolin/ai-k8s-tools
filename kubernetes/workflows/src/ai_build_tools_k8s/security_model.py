from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.security_research import ContractError, _load_json, _require, _require_sha256
from ai_build_tools_k8s.workflow import canonical_json, sha256_file, sha256_tree, write_json


SCHEMA_VERSION = "1.0.0"
STAGES = ("A", "B", "C")
SPLITS = {"train", "validation", "hidden", "adversarial"}
TARGET_TYPES = {
    "general-defense",
    "container-image",
    "test-site",
    "combined",
    "upstream-research",
}
REQUIRED_RELEASE_DIGESTS = (
    "foundation_digest",
    "adapter_digest",
    "tokenizer_digest",
    "chat_template_digest",
    "verification_plan_schema_digest",
    "finding_schema_digest",
    "policy_profile_digest",
)


def _record_digest(record: dict[str, Any]) -> str:
    material = {key: value for key, value in record.items() if key != "record_digest"}
    return f"sha256:{hashlib.sha256(canonical_json(material)).hexdigest()}"


def validate_dataset_record(record: dict[str, Any]) -> dict[str, Any]:
    for field in ("id", "stage", "split", "source", "license", "target_type", "messages"):
        _require(field in record, f"dataset record is missing {field}")
    _require(isinstance(record["id"], str) and record["id"], "record id is required")
    _require(record["stage"] in STAGES, "record stage must be A, B, or C")
    _require(record["split"] in SPLITS, "invalid dataset split")
    _require(record["target_type"] in TARGET_TYPES, "invalid target type")
    _require(isinstance(record["source"], str) and record["source"], "record source is required")
    _require(isinstance(record["license"], str) and record["license"], "record license is required")
    _require(record.get("permission_confirmed") is True, "record permission must be confirmed")
    messages = record["messages"]
    _require(isinstance(messages, list) and len(messages) >= 2, "record messages are incomplete")
    for message in messages:
        _require(message.get("role") in {"system", "user", "assistant"}, "invalid message role")
        _require(isinstance(message.get("content"), str) and message["content"], "empty message content")
    _require(messages[-1]["role"] == "assistant", "last message must be the training response")
    _require(sum(message["role"] == "assistant" for message in messages) == 1, "one assistant response is required")
    for field in ("evidence_ids", "allowed_operations", "forbidden_operations"):
        _require(isinstance(record.get(field), list), f"{field} must be a list")
    _require_sha256(str(record.get("record_digest", "")), "record_digest")
    _require(record["record_digest"] == _record_digest(record), "record digest mismatch")
    return record


def validate_dataset(manifest_path: Path, dataset_root: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "unsupported dataset schema")
    _require(manifest.get("license_review_complete") is True, "dataset license review is incomplete")
    records_name = manifest.get("records")
    _require(isinstance(records_name, str) and records_name, "dataset records path is required")
    records_path = (dataset_root / records_name).resolve()
    _require(dataset_root.resolve() in records_path.parents, "dataset records path escapes its root")
    _require(records_path.is_file(), f"dataset records do not exist: {records_path}")
    _require_sha256(str(manifest.get("records_digest", "")), "records_digest")
    observed_records_digest = f"sha256:{sha256_file(records_path)}"
    _require(observed_records_digest == manifest["records_digest"], "dataset records digest mismatch")

    records: list[dict[str, Any]] = []
    ids: set[str] = set()
    stage_counts = {stage: 0 for stage in STAGES}
    split_counts = {split: 0 for split in sorted(SPLITS)}
    for line_number, raw in enumerate(records_path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        _require(isinstance(value, dict), f"record {line_number} must be a JSON object")
        validate_dataset_record(value)
        _require(value["id"] not in ids, f"duplicate record id: {value['id']}")
        ids.add(value["id"])
        records.append(value)
        stage_counts[value["stage"]] += 1
        split_counts[value["split"]] += 1
    _require(records, "dataset is empty")
    _require(len(records) == int(manifest.get("record_count", -1)), "dataset record count mismatch")
    _require(stage_counts == manifest.get("stage_counts"), "dataset stage counts mismatch")
    _require(split_counts == manifest.get("split_counts"), "dataset split counts mismatch")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "dataset_name": manifest.get("dataset_name"),
        "dataset_manifest_digest": f"sha256:{sha256_file(manifest_path)}",
        "records_digest": observed_records_digest,
        "record_count": len(records),
        "stage_counts": stage_counts,
        "split_counts": split_counts,
    }


def validate_adviser_release(release: dict[str, Any]) -> dict[str, Any]:
    _require(release.get("schema_version") == SCHEMA_VERSION, "unsupported adviser release schema")
    _require(release.get("validation_level") == "AI_BLIND_REVIEWED", "release is not AI blind reviewed")
    _require(release.get("stage") == "C", "only Release C can be exported to the agent")
    for field in REQUIRED_RELEASE_DIGESTS:
        _require_sha256(str(release.get(field, "")), field)
    _require(release.get("serving_model_name") == "security-adviser-c", "unexpected serving model name")
    _require(int(release.get("lora_rank", 0)) > 0, "release LoRA rank is required")
    _require(
        set(release.get("supported_target_types", []))
        == {"container-image", "test-site", "combined", "upstream-research"},
        "release target types are incomplete",
    )
    _require(
        set(release.get("supported_research_selectors", []))
        == {"public-image", "public-source-repository", "public-source-runtime"},
        "release research selectors are incomplete",
    )
    return release


def create_adviser_release(
    foundation_digest: str,
    adapter: Path,
    tokenizer: Path,
    chat_template: Path,
    verification_plan_schema: Path,
    finding_schema: Path,
    policy_profile: Path,
    lora_rank: int,
) -> dict[str, Any]:
    _require(adapter.is_dir(), "adapter directory is required")
    _require(tokenizer.is_dir(), "tokenizer directory is required")
    for path in (chat_template, verification_plan_schema, finding_schema, policy_profile):
        _require(path.is_file(), f"release input does not exist: {path}")
    release = {
        "schema_version": SCHEMA_VERSION,
        "stage": "C",
        "validation_level": "AI_BLIND_REVIEWED",
        "foundation_digest": foundation_digest,
        "adapter_digest": f"sha256:{sha256_tree(adapter)}",
        "tokenizer_digest": f"sha256:{sha256_tree(tokenizer)}",
        "chat_template_digest": f"sha256:{sha256_file(chat_template)}",
        "verification_plan_schema_digest": f"sha256:{sha256_file(verification_plan_schema)}",
        "finding_schema_digest": f"sha256:{sha256_file(finding_schema)}",
        "policy_profile_digest": f"sha256:{sha256_file(policy_profile)}",
        "serving_model_name": "security-adviser-c",
        "lora_rank": lora_rank,
        "supported_target_types": ["combined", "container-image", "test-site", "upstream-research"],
        "supported_research_selectors": [
            "public-image",
            "public-source-repository",
            "public-source-runtime",
        ],
    }
    return validate_adviser_release(release)


def _require_image_digest(value: str, field: str) -> None:
    _require("@sha256:" in value, f"{field} must be digest-pinned")
    _require_sha256(f"sha256:{value.rsplit('@sha256:', 1)[1]}", field)


def render_security_training_job(
    name: str,
    namespace: str,
    trainer_image: str,
    pvc_name: str,
    config_path: str,
    gpu_count: int,
    node_selector_key: str,
    node_selector_value: str,
    image_pull_policy: str = "IfNotPresent",
    node_local_image_id: str = "",
) -> dict[str, Any]:
    _require(image_pull_policy in {"IfNotPresent", "Never"}, "unsupported image pull policy")
    if image_pull_policy == "Never":
        _require(":" in trainer_image and "@" not in trainer_image, "node-local image must use an explicit tag")
        _require_sha256(node_local_image_id, "node_local_image_id")
    else:
        _require_image_digest(trainer_image, "trainer_image")
        _require(not node_local_image_id, "node_local_image_id is only valid with imagePullPolicy Never")
    _require(gpu_count >= 1, "gpu_count must be positive")
    _require(config_path.startswith("/workspace/"), "training config must be on the workspace volume")
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "name": "trainer",
                "image": trainer_image,
                "imagePullPolicy": image_pull_policy,
                "command": ["torchrun"],
                "args": [
                    "--standalone",
                    "--nnodes=1",
                    f"--nproc-per-node={gpu_count}",
                    "/opt/ai-build-tools-security/trainer.py",
                    "--config",
                    config_path,
                ],
                "env": [
                    {"name": "HF_HUB_OFFLINE", "value": "1"},
                    {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                    {"name": "TOKENIZERS_PARALLELISM", "value": "false"},
                ],
                "resources": {
                    "requests": {"nvidia.com/gpu": gpu_count, "cpu": "8", "memory": "64Gi"},
                    "limits": {"nvidia.com/gpu": gpu_count, "cpu": "64", "memory": "512Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
            }
        ],
        "volumes": [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}},
            {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}},
        ],
    }
    if node_selector_key and node_selector_value:
        pod_spec["nodeSelector"] = {node_selector_key: node_selector_value}
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if node_local_image_id:
        metadata["annotations"] = {"ai-build-tools.ricolin.dev/node-local-image-id": node_local_image_id}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "backoffLimit": 0,
            "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
        },
    }


def render_adviser_inference_service(
    release: dict[str, Any],
    name: str,
    namespace: str,
    vllm_image: str,
    verifier_image: str,
    pvc_name: str,
    gpu_count: int,
    node_selector_key: str,
    node_selector_value: str,
) -> dict[str, Any]:
    validate_adviser_release(release)
    _require_image_digest(vllm_image, "vllm_image")
    _require_image_digest(verifier_image, "verifier_image")
    _require(gpu_count >= 1, "gpu_count must be positive")
    predictor: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "initContainers": [
            {
                "name": "verify-model-identities",
                "image": verifier_image,
                "command": ["ai-security-model"],
                "args": [
                    "verify-mounted-release",
                    "--release",
                    "/models/release/advisor-release.json",
                    "--foundation",
                    "/models/foundation",
                    "--adapter",
                    "/models/adapter",
                    "--tokenizer",
                    "/models/tokenizer",
                    "--chat-template",
                    "/models/release/chat-template.jinja",
                    "--verification-plan-schema",
                    "/models/release/verification-plan.schema.json",
                    "--finding-schema",
                    "/models/release/finding.schema.json",
                    "--policy-profile",
                    "/models/release/policy-profile.json",
                ],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                    "runAsNonRoot": True,
                },
                "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}],
            }
        ],
        "containers": [
            {
                "name": "kserve-container",
                "image": vllm_image,
                "imagePullPolicy": "IfNotPresent",
                "args": [
                    "--model",
                    "/models/foundation",
                    "--served-model-name",
                    "security-adviser-c",
                    "--enable-lora",
                    "--lora-modules",
                    "security-adviser-c=/models/adapter",
                    "--max-lora-rank",
                    str(release["lora_rank"]),
                    "--tokenizer",
                    "/models/tokenizer",
                    "--tensor-parallel-size",
                    str(gpu_count),
                ],
                "ports": [{"name": "http1", "containerPort": 8000, "protocol": "TCP"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
                "resources": {
                    "requests": {"nvidia.com/gpu": gpu_count, "cpu": "4", "memory": "32Gi"},
                    "limits": {"nvidia.com/gpu": gpu_count, "cpu": "32", "memory": "256Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}],
            }
        ],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": pvc_name}}],
    }
    if node_selector_key and node_selector_value:
        predictor["nodeSelector"] = {node_selector_key: node_selector_value}
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "serving.kserve.io/deploymentMode": "Standard",
                "ai-build-tools.ricolin.dev/adapter-digest": release["adapter_digest"],
            },
        },
        "spec": {"predictor": predictor},
    }


def verify_mounted_release(
    release_path: Path,
    foundation: Path,
    adapter: Path,
    tokenizer: Path,
    chat_template: Path,
    verification_plan_schema: Path,
    finding_schema: Path,
    policy_profile: Path,
) -> dict[str, Any]:
    release = validate_adviser_release(_load_json(release_path))
    observed = {
        "foundation_digest": f"sha256:{sha256_tree(foundation)}",
        "adapter_digest": f"sha256:{sha256_tree(adapter)}",
        "tokenizer_digest": f"sha256:{sha256_tree(tokenizer)}",
        "chat_template_digest": f"sha256:{sha256_file(chat_template)}",
        "verification_plan_schema_digest": f"sha256:{sha256_file(verification_plan_schema)}",
        "finding_schema_digest": f"sha256:{sha256_file(finding_schema)}",
        "policy_profile_digest": f"sha256:{sha256_file(policy_profile)}",
    }
    mismatches = [field for field, value in observed.items() if release[field] != value]
    _require(not mismatches, f"mounted release digest mismatch: {', '.join(mismatches)}")
    return {"schema_version": SCHEMA_VERSION, "status": "PASS", "observed": observed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Security adviser dataset, training, and serving contracts")
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("validate-dataset")
    dataset.add_argument("--manifest", required=True)
    dataset.add_argument("--dataset-root", required=True)
    dataset.add_argument("--output", required=True)

    release = commands.add_parser("create-adviser-release")
    release.add_argument("--foundation-digest", required=True)
    release.add_argument("--adapter", required=True)
    release.add_argument("--tokenizer", required=True)
    release.add_argument("--chat-template", required=True)
    release.add_argument("--verification-plan-schema", required=True)
    release.add_argument("--finding-schema", required=True)
    release.add_argument("--policy-profile", required=True)
    release.add_argument("--lora-rank", type=int, required=True)
    release.add_argument("--output", required=True)

    validate_release = commands.add_parser("validate-adviser-release")
    validate_release.add_argument("--release", required=True)

    mounted = commands.add_parser("verify-mounted-release")
    mounted.add_argument("--release", required=True)
    mounted.add_argument("--foundation", required=True)
    mounted.add_argument("--adapter", required=True)
    mounted.add_argument("--tokenizer", required=True)
    mounted.add_argument("--chat-template", required=True)
    mounted.add_argument("--verification-plan-schema", required=True)
    mounted.add_argument("--finding-schema", required=True)
    mounted.add_argument("--policy-profile", required=True)
    mounted.add_argument("--output", default="")

    training = commands.add_parser("render-training-job")
    training.add_argument("--name", required=True)
    training.add_argument("--namespace", required=True)
    training.add_argument("--trainer-image", required=True)
    training.add_argument("--pvc", required=True)
    training.add_argument("--config-path", required=True)
    training.add_argument("--gpu-count", type=int, required=True)
    training.add_argument("--node-selector-key", default="")
    training.add_argument("--node-selector-value", default="")
    training.add_argument("--image-pull-policy", choices=("IfNotPresent", "Never"), default="IfNotPresent")
    training.add_argument("--node-local-image-id", default="")
    training.add_argument("--output", required=True)

    serving = commands.add_parser("render-adviser-serving")
    serving.add_argument("--release", required=True)
    serving.add_argument("--name", required=True)
    serving.add_argument("--namespace", required=True)
    serving.add_argument("--vllm-image", required=True)
    serving.add_argument("--verifier-image", required=True)
    serving.add_argument("--pvc", required=True)
    serving.add_argument("--gpu-count", type=int, required=True)
    serving.add_argument("--node-selector-key", default="")
    serving.add_argument("--node-selector-value", default="")
    serving.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate-dataset":
            write_json(Path(args.output), validate_dataset(Path(args.manifest), Path(args.dataset_root)))
        elif args.command == "create-adviser-release":
            value = create_adviser_release(
                args.foundation_digest,
                Path(args.adapter),
                Path(args.tokenizer),
                Path(args.chat_template),
                Path(args.verification_plan_schema),
                Path(args.finding_schema),
                Path(args.policy_profile),
                args.lora_rank,
            )
            write_json(Path(args.output), value)
        elif args.command == "validate-adviser-release":
            validate_adviser_release(_load_json(Path(args.release)))
        elif args.command == "verify-mounted-release":
            value = verify_mounted_release(
                Path(args.release),
                Path(args.foundation),
                Path(args.adapter),
                Path(args.tokenizer),
                Path(args.chat_template),
                Path(args.verification_plan_schema),
                Path(args.finding_schema),
                Path(args.policy_profile),
            )
            if args.output:
                write_json(Path(args.output), value)
        elif args.command == "render-training-job":
            write_json(
                Path(args.output),
                render_security_training_job(
                    args.name,
                    args.namespace,
                    args.trainer_image,
                    args.pvc,
                    args.config_path,
                    args.gpu_count,
                    args.node_selector_key,
                    args.node_selector_value,
                    args.image_pull_policy,
                    args.node_local_image_id,
                ),
            )
        elif args.command == "render-adviser-serving":
            write_json(
                Path(args.output),
                render_adviser_inference_service(
                    _load_json(Path(args.release)),
                    args.name,
                    args.namespace,
                    args.vllm_image,
                    args.verifier_image,
                    args.pvc,
                    args.gpu_count,
                    args.node_selector_key,
                    args.node_selector_value,
                ),
            )
    except ContractError as error:
        raise SystemExit(f"contract error: {error}") from error


if __name__ == "__main__":
    main()

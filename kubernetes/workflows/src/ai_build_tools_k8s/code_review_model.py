from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.workflow import (
    add_control_plane_tolerations,
    canonical_json,
    sha256_file,
    sha256_tree,
    write_json,
)


SCHEMA_VERSION = "1.0.0"
STAGES = ("A", "B", "C")
SPLITS = {"train", "validation", "hidden", "adversarial"}
TARGET_TYPES = {"single-file", "pull-request", "repository", "agent-plan"}
LANGUAGES = {"bash", "python", "go", "rust", "yaml"}
REQUIRED_RELEASE_DIGESTS = (
    "foundation_digest",
    "adapter_digest",
    "tokenizer_digest",
    "chat_template_digest",
    "review_schema_digest",
    "agent_plan_schema_digest",
    "policy_profile_digest",
)


class ContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def require_sha256(value: str, field: str) -> None:
    require(
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{field} must be a lowercase sha256 digest",
    )


def record_digest(record: dict[str, Any]) -> str:
    material = {key: value for key, value in record.items() if key != "record_digest"}
    return f"sha256:{hashlib.sha256(canonical_json(material)).hexdigest()}"


def validate_dataset_record(record: dict[str, Any]) -> dict[str, Any]:
    required = (
        "id",
        "stage",
        "split",
        "source",
        "license",
        "target_type",
        "languages",
        "messages",
    )
    for field in required:
        require(field in record, f"dataset record is missing {field}")
    require(isinstance(record["id"], str) and record["id"], "record id is required")
    require(record["stage"] in STAGES, "record stage must be A, B, or C")
    require(record["split"] in SPLITS, "invalid dataset split")
    require(record["target_type"] in TARGET_TYPES, "invalid target type")
    languages = record["languages"]
    require(isinstance(languages, list) and languages, "record languages are required")
    require(set(languages) <= LANGUAGES, "record contains an unsupported language")
    require(record.get("permission_confirmed") is True, "record permission must be confirmed")
    require(isinstance(record["source"], str) and record["source"], "record source is required")
    require(isinstance(record["license"], str) and record["license"], "record license is required")
    messages = record["messages"]
    require(isinstance(messages, list) and len(messages) >= 2, "record messages are incomplete")
    for message in messages:
        require(message.get("role") in {"system", "user", "assistant"}, "invalid message role")
        require(isinstance(message.get("content"), str) and message["content"], "empty message content")
    require(messages[-1]["role"] == "assistant", "last message must be the training response")
    require(sum(message["role"] == "assistant" for message in messages) == 1, "one assistant response is required")
    require_sha256(str(record.get("record_digest", "")), "record_digest")
    require(record["record_digest"] == record_digest(record), "record digest mismatch")
    return record


def validate_dataset(manifest_path: Path, dataset_root: Path) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    require(manifest.get("schema_version") == SCHEMA_VERSION, "unsupported dataset schema")
    require(manifest.get("license_review_complete") is True, "dataset license review is incomplete")
    records_name = manifest.get("records")
    require(isinstance(records_name, str) and records_name, "dataset records path is required")
    records_path = (dataset_root / records_name).resolve()
    require(dataset_root.resolve() in records_path.parents, "dataset records path escapes its root")
    require(records_path.is_file(), f"dataset records do not exist: {records_path}")
    require_sha256(str(manifest.get("records_digest", "")), "records_digest")
    observed_digest = f"sha256:{sha256_file(records_path)}"
    require(observed_digest == manifest["records_digest"], "dataset records digest mismatch")

    ids: set[str] = set()
    stage_counts = {stage: 0 for stage in STAGES}
    split_counts = {split: 0 for split in sorted(SPLITS)}
    language_counts = {language: 0 for language in sorted(LANGUAGES)}
    records = []
    for line_number, raw in enumerate(records_path.read_text().splitlines(), start=1):
        if not raw.strip():
            continue
        record = json.loads(raw)
        require(isinstance(record, dict), f"record {line_number} must be a JSON object")
        validate_dataset_record(record)
        require(record["id"] not in ids, f"duplicate record id: {record['id']}")
        ids.add(record["id"])
        records.append(record)
        stage_counts[record["stage"]] += 1
        split_counts[record["split"]] += 1
        for language in record["languages"]:
            language_counts[language] += 1
    require(records, "dataset is empty")
    require(len(records) == int(manifest.get("record_count", -1)), "dataset record count mismatch")
    require(stage_counts == manifest.get("stage_counts"), "dataset stage counts mismatch")
    require(split_counts == manifest.get("split_counts"), "dataset split counts mismatch")
    require(language_counts == manifest.get("language_counts"), "dataset language counts mismatch")
    require(all(language_counts.values()), "every supported language requires records")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "dataset_name": manifest.get("dataset_name"),
        "dataset_manifest_digest": f"sha256:{sha256_file(manifest_path)}",
        "records_digest": observed_digest,
        "record_count": len(records),
        "stage_counts": stage_counts,
        "split_counts": split_counts,
        "language_counts": language_counts,
    }


def validate_release(release: dict[str, Any]) -> dict[str, Any]:
    require(release.get("schema_version") == SCHEMA_VERSION, "unsupported release schema")
    require(release.get("stage") == "C", "only Release C can be exported")
    require(release.get("validation_level") == "AUTOMATED_ACCEPTED", "release is not accepted")
    require(release.get("serving_model_name") == "code-reviewer-c", "unexpected serving model name")
    require(set(release.get("supported_languages", [])) == LANGUAGES, "release languages are incomplete")
    require(set(release.get("supported_target_types", [])) == TARGET_TYPES, "release target types are incomplete")
    require(int(release.get("lora_rank", 0)) > 0, "release LoRA rank is required")
    for field in REQUIRED_RELEASE_DIGESTS:
        require_sha256(str(release.get(field, "")), field)
    return release


def create_release(
    foundation_digest: str,
    adapter: Path,
    tokenizer: Path,
    chat_template: Path,
    review_schema: Path,
    agent_plan_schema: Path,
    policy_profile: Path,
    lora_rank: int,
) -> dict[str, Any]:
    require_sha256(foundation_digest, "foundation_digest")
    require(adapter.is_dir(), "adapter directory is required")
    require(tokenizer.is_dir(), "tokenizer directory is required")
    for path in (chat_template, review_schema, agent_plan_schema, policy_profile):
        require(path.is_file(), f"release input does not exist: {path}")
    release = {
        "schema_version": SCHEMA_VERSION,
        "stage": "C",
        "validation_level": "AUTOMATED_ACCEPTED",
        "foundation_digest": foundation_digest,
        "adapter_digest": f"sha256:{sha256_tree(adapter)}",
        "tokenizer_digest": f"sha256:{sha256_tree(tokenizer)}",
        "chat_template_digest": f"sha256:{sha256_file(chat_template)}",
        "review_schema_digest": f"sha256:{sha256_file(review_schema)}",
        "agent_plan_schema_digest": f"sha256:{sha256_file(agent_plan_schema)}",
        "policy_profile_digest": f"sha256:{sha256_file(policy_profile)}",
        "serving_model_name": "code-reviewer-c",
        "lora_rank": lora_rank,
        "supported_languages": sorted(LANGUAGES),
        "supported_target_types": sorted(TARGET_TYPES),
    }
    return validate_release(release)


def require_image(value: str, field: str) -> None:
    require("@sha256:" in value, f"{field} must be digest-pinned")
    require_sha256(f"sha256:{value.rsplit('@sha256:', 1)[1]}", field)


def render_training_job(
    name: str,
    namespace: str,
    trainer_image: str,
    pvc_name: str,
    config_path: str,
    gpu_count: int,
    node_selector_key: str,
    node_selector_value: str,
    image_pull_policy: str,
    node_local_image_id: str,
    tolerate_control_plane: bool = False,
) -> dict[str, Any]:
    require(image_pull_policy in {"IfNotPresent", "Never"}, "unsupported image pull policy")
    if image_pull_policy == "Never":
        require(":" in trainer_image and "@" not in trainer_image, "node-local image requires a tag")
        require_sha256(node_local_image_id, "node_local_image_id")
    else:
        require_image(trainer_image, "trainer_image")
        require(not node_local_image_id, "node_local_image_id is only valid with Never")
    require(gpu_count >= 1, "gpu_count must be positive")
    require(config_path.startswith("/workspace/"), "training config must be on the workspace volume")
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
                    "/opt/ai-code-review/trainer.py",
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
    add_control_plane_tolerations(pod_spec, tolerate_control_plane)
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if node_local_image_id:
        metadata["annotations"] = {"ai-k8s-tools.ricolin.dev/node-local-image-id": node_local_image_id}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec}},
    }


def render_node_local_serving(
    release: dict[str, Any],
    name: str,
    namespace: str,
    serving_image: str,
    pvc_name: str,
    foundation_path: str,
    adapter_path: str,
    node_selector_key: str,
    node_selector_value: str,
    image_pull_policy: str,
    node_local_image_id: str,
    tolerate_control_plane: bool = False,
) -> dict[str, Any]:
    validate_release(release)
    require(image_pull_policy in {"IfNotPresent", "Never"}, "unsupported image pull policy")
    if image_pull_policy == "Never":
        require(":" in serving_image and "@" not in serving_image, "node-local image requires a tag")
        require_sha256(node_local_image_id, "node_local_image_id")
    else:
        require_image(serving_image, "serving_image")
        require(not node_local_image_id, "node_local_image_id is only valid with Never")
    for value, field in ((foundation_path, "foundation_path"), (adapter_path, "adapter_path")):
        require(value.startswith("/workspace/"), f"{field} must be on the workspace volume")

    predictor: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "minReplicas": 1,
        "maxReplicas": 1,
        "containers": [
            {
                "name": "kserve-container",
                "image": serving_image,
                "imagePullPolicy": image_pull_policy,
                "command": ["/opt/ai-venv/bin/python", "/opt/ai-code-review/serve_reviewer.py"],
                "args": [
                    "--foundation", foundation_path,
                    "--adapter", adapter_path,
                    "--foundation-digest", release["foundation_digest"],
                    "--adapter-digest", release["adapter_digest"],
                    "--model-name", release["serving_model_name"],
                    "--max-new-tokens", "2048",
                    "--port", "8000",
                ],
                "env": [
                    {"name": "HF_HUB_OFFLINE", "value": "1"},
                    {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                    {"name": "TOKENIZERS_PARALLELISM", "value": "false"},
                ],
                "ports": [{"name": "http1", "containerPort": 8000, "protocol": "TCP"}],
                "startupProbe": {
                    "httpGet": {"path": "/health", "port": 8000},
                    "periodSeconds": 10,
                    "failureThreshold": 90,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/health", "port": 8000},
                    "periodSeconds": 10,
                    "failureThreshold": 6,
                },
                "livenessProbe": {
                    "httpGet": {"path": "/health", "port": 8000},
                    "periodSeconds": 30,
                    "failureThreshold": 3,
                },
                "resources": {
                    "requests": {"nvidia.com/gpu": 1, "cpu": "4", "memory": "64Gi"},
                    "limits": {"nvidia.com/gpu": 1, "cpu": "32", "memory": "256Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                    "runAsUser": 65532,
                    "runAsGroup": 65532,
                },
                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace", "readOnly": True}],
            }
        ],
        "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": pvc_name}}],
    }
    if node_selector_key and node_selector_value:
        predictor["nodeSelector"] = {node_selector_key: node_selector_value}
    add_control_plane_tolerations(predictor, tolerate_control_plane)
    annotations = {
        "serving.kserve.io/deploymentMode": "Standard",
        "ai-k8s-tools.ricolin.dev/foundation-digest": release["foundation_digest"],
        "ai-k8s-tools.ricolin.dev/adapter-digest": release["adapter_digest"],
    }
    if node_local_image_id:
        annotations["ai-k8s-tools.ricolin.dev/node-local-image-id"] = node_local_image_id
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": name, "namespace": namespace, "annotations": annotations},
        "spec": {"predictor": predictor},
    }


def render_serving(
    release: dict[str, Any],
    name: str,
    namespace: str,
    vllm_image: str,
    verifier_image: str,
    pvc_name: str,
    gpu_count: int,
    node_selector_key: str,
    node_selector_value: str,
    tolerate_control_plane: bool = False,
) -> dict[str, Any]:
    validate_release(release)
    require_image(vllm_image, "vllm_image")
    require_image(verifier_image, "verifier_image")
    predictor: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "initContainers": [
            {
                "name": "verify-model-identities",
                "image": verifier_image,
                "command": ["ai-code-review-model"],
                "args": [
                    "verify-mounted-release",
                    "--release", "/models/release/code-review-release.json",
                    "--foundation", "/models/foundation",
                    "--adapter", "/models/adapter",
                    "--tokenizer", "/models/tokenizer",
                    "--chat-template", "/models/release/chat-template.jinja",
                    "--review-schema", "/models/release/review.schema.json",
                    "--agent-plan-schema", "/models/release/agent-plan.schema.json",
                    "--policy-profile", "/models/release/policy-profile.json",
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
                    "--model", "/models/foundation",
                    "--served-model-name", "code-reviewer-c",
                    "--enable-lora",
                    "--lora-modules", "code-reviewer-c=/models/adapter",
                    "--max-lora-rank", str(release["lora_rank"]),
                    "--tokenizer", "/models/tokenizer",
                    "--tensor-parallel-size", str(gpu_count),
                ],
                "ports": [{"name": "http1", "containerPort": 8000, "protocol": "TCP"}],
                "readinessProbe": {"httpGet": {"path": "/health", "port": 8000}},
                "resources": {
                    "requests": {"nvidia.com/gpu": gpu_count, "cpu": "4", "memory": "32Gi"},
                    "limits": {"nvidia.com/gpu": gpu_count, "cpu": "32", "memory": "256Gi"},
                },
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True}],
            }
        ],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": pvc_name}}],
    }
    if node_selector_key and node_selector_value:
        predictor["nodeSelector"] = {node_selector_key: node_selector_value}
    add_control_plane_tolerations(predictor, tolerate_control_plane)
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "serving.kserve.io/deploymentMode": "Standard",
                "ai-k8s-tools.ricolin.dev/adapter-digest": release["adapter_digest"],
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
    review_schema: Path,
    agent_plan_schema: Path,
    policy_profile: Path,
) -> dict[str, Any]:
    release = validate_release(load_json(release_path))
    observed = {
        "foundation_digest": f"sha256:{sha256_tree(foundation)}",
        "adapter_digest": f"sha256:{sha256_tree(adapter)}",
        "tokenizer_digest": f"sha256:{sha256_tree(tokenizer)}",
        "chat_template_digest": f"sha256:{sha256_file(chat_template)}",
        "review_schema_digest": f"sha256:{sha256_file(review_schema)}",
        "agent_plan_schema_digest": f"sha256:{sha256_file(agent_plan_schema)}",
        "policy_profile_digest": f"sha256:{sha256_file(policy_profile)}",
    }
    mismatches = [field for field, value in observed.items() if release[field] != value]
    require(not mismatches, f"mounted release digest mismatch: {', '.join(mismatches)}")
    return {"schema_version": SCHEMA_VERSION, "status": "PASS", "observed": observed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Code-review model dataset, training, release, and serving contracts")
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("validate-dataset")
    dataset.add_argument("--manifest", required=True)
    dataset.add_argument("--dataset-root", required=True)
    dataset.add_argument("--output", required=True)

    release = commands.add_parser("create-release")
    release.add_argument("--foundation-digest", required=True)
    release.add_argument("--adapter", required=True)
    release.add_argument("--tokenizer", required=True)
    release.add_argument("--chat-template", required=True)
    release.add_argument("--review-schema", required=True)
    release.add_argument("--agent-plan-schema", required=True)
    release.add_argument("--policy-profile", required=True)
    release.add_argument("--lora-rank", type=int, required=True)
    release.add_argument("--output", required=True)

    validate = commands.add_parser("validate-release")
    validate.add_argument("--release", required=True)

    mounted = commands.add_parser("verify-mounted-release")
    mounted.add_argument("--release", required=True)
    mounted.add_argument("--foundation", required=True)
    mounted.add_argument("--adapter", required=True)
    mounted.add_argument("--tokenizer", required=True)
    mounted.add_argument("--chat-template", required=True)
    mounted.add_argument("--review-schema", required=True)
    mounted.add_argument("--agent-plan-schema", required=True)
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
    training.add_argument("--tolerate-control-plane", action="store_true")
    training.add_argument("--output", required=True)

    serving = commands.add_parser("render-serving")
    serving.add_argument("--release", required=True)
    serving.add_argument("--name", required=True)
    serving.add_argument("--namespace", required=True)
    serving.add_argument("--vllm-image", required=True)
    serving.add_argument("--verifier-image", required=True)
    serving.add_argument("--pvc", required=True)
    serving.add_argument("--gpu-count", type=int, required=True)
    serving.add_argument("--node-selector-key", default="")
    serving.add_argument("--node-selector-value", default="")
    serving.add_argument("--tolerate-control-plane", action="store_true")
    serving.add_argument("--output", required=True)

    node_serving = commands.add_parser("render-node-local-serving")
    node_serving.add_argument("--release", required=True)
    node_serving.add_argument("--name", required=True)
    node_serving.add_argument("--namespace", required=True)
    node_serving.add_argument("--serving-image", required=True)
    node_serving.add_argument("--pvc", required=True)
    node_serving.add_argument("--foundation-path", required=True)
    node_serving.add_argument("--adapter-path", required=True)
    node_serving.add_argument("--node-selector-key", default="")
    node_serving.add_argument("--node-selector-value", default="")
    node_serving.add_argument("--image-pull-policy", choices=("IfNotPresent", "Never"), default="IfNotPresent")
    node_serving.add_argument("--node-local-image-id", default="")
    node_serving.add_argument("--tolerate-control-plane", action="store_true")
    node_serving.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate-dataset":
            write_json(Path(args.output), validate_dataset(Path(args.manifest), Path(args.dataset_root)))
        elif args.command == "create-release":
            write_json(
                Path(args.output),
                create_release(
                    args.foundation_digest,
                    Path(args.adapter),
                    Path(args.tokenizer),
                    Path(args.chat_template),
                    Path(args.review_schema),
                    Path(args.agent_plan_schema),
                    Path(args.policy_profile),
                    args.lora_rank,
                ),
            )
        elif args.command == "validate-release":
            validate_release(load_json(Path(args.release)))
        elif args.command == "verify-mounted-release":
            result = verify_mounted_release(
                Path(args.release),
                Path(args.foundation),
                Path(args.adapter),
                Path(args.tokenizer),
                Path(args.chat_template),
                Path(args.review_schema),
                Path(args.agent_plan_schema),
                Path(args.policy_profile),
            )
            if args.output:
                write_json(Path(args.output), result)
        elif args.command == "render-training-job":
            write_json(
                Path(args.output),
                render_training_job(
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
                    args.tolerate_control_plane,
                ),
            )
        elif args.command == "render-serving":
            write_json(
                Path(args.output),
                render_serving(
                    load_json(Path(args.release)),
                    args.name,
                    args.namespace,
                    args.vllm_image,
                    args.verifier_image,
                    args.pvc,
                    args.gpu_count,
                    args.node_selector_key,
                    args.node_selector_value,
                    args.tolerate_control_plane,
                ),
            )
        elif args.command == "render-node-local-serving":
            write_json(
                Path(args.output),
                render_node_local_serving(
                    load_json(Path(args.release)),
                    args.name,
                    args.namespace,
                    args.serving_image,
                    args.pvc,
                    args.foundation_path,
                    args.adapter_path,
                    args.node_selector_key,
                    args.node_selector_value,
                    args.image_pull_policy,
                    args.node_local_image_id,
                    args.tolerate_control_plane,
                ),
            )
    except ContractError as error:
        raise SystemExit(f"contract error: {error}") from error


if __name__ == "__main__":
    main()

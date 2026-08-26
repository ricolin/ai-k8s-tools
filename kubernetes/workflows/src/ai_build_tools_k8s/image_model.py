from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.security_research import ContractError, _load_json, _require, _require_sha256
from ai_build_tools_k8s.workflow import add_control_plane_tolerations, sha256_file, write_json


def require_image_digest(value: str, field: str) -> None:
    _require("@sha256:" in value, f"{field} must be digest-pinned")
    _require_sha256(f"sha256:{value.rsplit('@sha256:', 1)[1]}", field)


def validate_comparison_prompts(path: Path) -> dict[str, Any]:
    values = _load_json(path) if path.read_text().lstrip().startswith("{") else None
    if values is not None:
        records = values.get("prompts")
    else:
        import json

        records = json.loads(path.read_text())
    _require(isinstance(records, list) and len(records) >= 3, "at least three comparison prompts are required")
    ids: set[str] = set()
    for record in records:
        required = {"id", "prompt", "negative_prompt", "seed", "width", "height", "steps", "guidance"}
        _require(not (required - set(record)), f"comparison prompt is missing {sorted(required - set(record))}")
        _require(record["id"] not in ids, f"duplicate prompt id: {record['id']}")
        ids.add(record["id"])
        _require(int(record["width"]) == 1024 and int(record["height"]) == 1024, "comparison prompts must be 1024x1024")
        _require(isinstance(record["prompt"], str) and record["prompt"], "comparison prompt is empty")
        _require(isinstance(record["negative_prompt"], str), "negative prompt must be text")
    return {
        "schema_version": "1.0.0",
        "status": "PASS",
        "prompt_count": len(records),
        "prompt_ids": sorted(ids),
        "prompt_manifest_digest": f"sha256:{sha256_file(path)}",
    }


def _pod_security() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
    }


def render_image_job(
    name: str,
    namespace: str,
    image: str,
    pvc: str,
    config_path: str,
    gpu_count: int,
    mode: str,
    node_selector_key: str,
    node_selector_value: str,
    image_pull_policy: str = "IfNotPresent",
    node_local_image_id: str = "",
    tolerate_control_plane: bool = False,
    queue_name: str = "",
) -> dict[str, Any]:
    _require(image_pull_policy in {"IfNotPresent", "Never"}, "unsupported image pull policy")
    if image_pull_policy == "Never":
        _require(":" in image and "@" not in image, "node-local image must use an explicit tag")
        _require_sha256(node_local_image_id, "node_local_image_id")
    else:
        require_image_digest(image, "image")
        _require(not node_local_image_id, "node_local_image_id is only valid with imagePullPolicy Never")
    _require(mode in {"train", "generate"}, "unsupported image job mode")
    _require(gpu_count >= 1, "gpu_count must be positive")
    _require(config_path.startswith("/workspace/"), "config must be on the workspace PVC")
    script = "train_stage.py" if mode == "train" else "generate_comparison.py"
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": mode,
                "image": image,
                "imagePullPolicy": image_pull_policy,
                "command": ["python", f"/opt/ai-build-tools-image/{script}"],
                "args": ["--config", config_path],
                "env": [
                    {"name": "HF_HUB_OFFLINE", "value": "1"},
                    {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                    {"name": "HF_DATASETS_OFFLINE", "value": "1"},
                ],
                "resources": {
                    "requests": {"nvidia.com/gpu": gpu_count, "cpu": "8", "memory": "64Gi"},
                    "limits": {"nvidia.com/gpu": gpu_count, "cpu": "64", "memory": "512Gi"},
                },
                "securityContext": _pod_security(),
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
            }
        ],
        "volumes": [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}},
            {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}},
        ],
    }
    if node_selector_key and node_selector_value:
        pod_spec["nodeSelector"] = {node_selector_key: node_selector_value}
    add_control_plane_tolerations(pod_spec, tolerate_control_plane)
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    if queue_name:
        metadata["labels"] = {"kueue.x-k8s.io/queue-name": queue_name}
    if node_local_image_id:
        metadata["annotations"] = {"ai-build-tools.ricolin.dev/node-local-image-id": node_local_image_id}
    job_spec: dict[str, Any] = {
        "backoffLimit": 0,
        "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
    }
    if queue_name:
        job_spec["suspend"] = True
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": job_spec,
    }


def render_image_trainjob(
    name: str,
    namespace: str,
    image: str,
    pvc: str,
    config_path: str,
    gpu_count: int,
    queue_name: str,
    runtime_name: str,
    node_selector_key: str,
    node_selector_value: str,
    image_pull_policy: str = "IfNotPresent",
    node_local_image_id: str = "",
    tolerate_control_plane: bool = False,
) -> dict[str, Any]:
    _require(image_pull_policy in {"IfNotPresent", "Never"}, "unsupported image pull policy")
    if image_pull_policy == "Never":
        _require(":" in image and "@" not in image, "node-local image must use an explicit tag")
        _require_sha256(node_local_image_id, "node_local_image_id")
    else:
        require_image_digest(image, "image")
        _require(not node_local_image_id, "node_local_image_id is only valid with imagePullPolicy Never")
    _require(gpu_count >= 1, "gpu_count must be positive")
    _require(config_path.startswith("/workspace/"), "config must be on the workspace PVC")
    _require(queue_name, "queue_name is required")
    _require(runtime_name, "runtime_name is required")

    pod_patch: dict[str, Any] = {
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "node",
                "securityContext": _pod_security(),
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "dshm", "mountPath": "/dev/shm"},
                ],
            }
        ],
        "volumes": [
            {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}},
            {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "32Gi"}},
        ],
    }
    if node_selector_key and node_selector_value:
        pod_patch["nodeSelector"] = {node_selector_key: node_selector_value}
    add_control_plane_tolerations(pod_patch, tolerate_control_plane)

    annotations = {"ai-build-tools.ricolin.dev/training-primitive": "kubeflow-trainer-v2"}
    if node_local_image_id:
        annotations["ai-build-tools.ricolin.dev/node-local-image-id"] = node_local_image_id
    return {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"kueue.x-k8s.io/queue-name": queue_name},
            "annotations": annotations,
        },
        "spec": {
            "activeDeadlineSeconds": 14400,
            "runtimeRef": {
                "apiGroup": "trainer.kubeflow.org",
                "kind": "ClusterTrainingRuntime",
                "name": runtime_name,
            },
            "trainer": {
                "numNodes": 1,
                "numProcPerNode": 1,
                "image": image,
                "command": ["python"],
                "args": ["/opt/ai-build-tools-image/train_stage.py", "--config", config_path],
                "env": [
                    {"name": "HF_HUB_OFFLINE", "value": "1"},
                    {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                    {"name": "HF_DATASETS_OFFLINE", "value": "1"},
                ],
                "resourcesPerNode": {
                    "requests": {"nvidia.com/gpu": gpu_count, "cpu": "8", "memory": "64Gi"},
                    "limits": {"nvidia.com/gpu": gpu_count, "cpu": "64", "memory": "512Gi"},
                },
            },
            "runtimePatches": [
                {
                    "manager": "ai-k8s-tools.ricolin.dev/image-workflow",
                    "trainingRuntimeSpec": {
                        "template": {
                            "spec": {
                                "replicatedJobs": [
                                    {
                                        "name": "node",
                                        "template": {
                                            "spec": {"template": {"spec": pod_patch}},
                                        },
                                    }
                                ]
                            }
                        }
                    },
                }
            ],
        },
    }


def create_release_manifest(
    name: str,
    base_digest: str,
    adapter_records: list[dict[str, Any]],
    prompt_digest: str,
    evaluation_digest: str,
    validation_level: str,
) -> dict[str, Any]:
    _require(
        name in {
            "release-a-watercolor",
            "release-b-watercolor-detail",
            "release-b-watercolor-impressionism",
            "release-c-watercolor-complex",
        },
        "invalid release name",
    )
    for field, value in (
        ("base_digest", base_digest),
        ("prompt_digest", prompt_digest),
        ("evaluation_digest", evaluation_digest),
    ):
        _require_sha256(value, field)
    _require(validation_level in {"AUTOMATED_ACCEPTED", "AI_BLIND_REVIEWED"}, "invalid validation level")
    expected_adapters = {
        "release-a-watercolor": ["watercolor"],
        "release-b-watercolor-detail": ["watercolor", "detail"],
        "release-b-watercolor-impressionism": ["watercolor", "impressionism"],
        "release-c-watercolor-complex": ["c-watercolor", "c-complex-detail"],
    }
    _require(
        [record.get("name") for record in adapter_records] == expected_adapters[name],
        "release adapter composition or order is invalid",
    )
    for adapter in adapter_records:
        _require(adapter.get("name") and isinstance(adapter.get("scale"), (int, float)), "invalid adapter record")
        _require_sha256(str(adapter.get("digest", "")), "adapter digest")
    return {
        "schema_version": "1.0.0",
        "release_name": name,
        "base_digest": base_digest,
        "adapters": adapter_records,
        "comparison_prompt_digest": prompt_digest,
        "evaluation_digest": evaluation_digest,
        "validation_level": validation_level,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SDXL CUDA image workflow contracts")
    commands = parser.add_subparsers(dest="command", required=True)
    prompts = commands.add_parser("validate-prompts")
    prompts.add_argument("--prompts", required=True)
    prompts.add_argument("--output", required=True)
    job = commands.add_parser("render-job")
    job.add_argument("--name", required=True)
    job.add_argument("--namespace", required=True)
    job.add_argument("--image", required=True)
    job.add_argument("--pvc", required=True)
    job.add_argument("--config-path", required=True)
    job.add_argument("--gpu-count", required=True, type=int)
    job.add_argument("--mode", choices=("train", "generate"), required=True)
    job.add_argument("--node-selector-key", default="")
    job.add_argument("--node-selector-value", default="")
    job.add_argument("--image-pull-policy", choices=("IfNotPresent", "Never"), default="IfNotPresent")
    job.add_argument("--node-local-image-id", default="")
    job.add_argument("--tolerate-control-plane", action="store_true")
    job.add_argument("--queue", default="")
    job.add_argument("--output", required=True)

    trainjob = commands.add_parser("render-trainjob")
    trainjob.add_argument("--name", required=True)
    trainjob.add_argument("--namespace", required=True)
    trainjob.add_argument("--image", required=True)
    trainjob.add_argument("--pvc", required=True)
    trainjob.add_argument("--config-path", required=True)
    trainjob.add_argument("--gpu-count", required=True, type=int)
    trainjob.add_argument("--queue", default="ai-workflows")
    trainjob.add_argument("--runtime", default="torch-distributed")
    trainjob.add_argument("--node-selector-key", default="")
    trainjob.add_argument("--node-selector-value", default="")
    trainjob.add_argument("--image-pull-policy", choices=("IfNotPresent", "Never"), default="IfNotPresent")
    trainjob.add_argument("--node-local-image-id", default="")
    trainjob.add_argument("--tolerate-control-plane", action="store_true")
    trainjob.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate-prompts":
            write_json(Path(args.output), validate_comparison_prompts(Path(args.prompts)))
        elif args.command == "render-job":
            write_json(
                Path(args.output),
                render_image_job(
                    args.name,
                    args.namespace,
                    args.image,
                    args.pvc,
                    args.config_path,
                    args.gpu_count,
                    args.mode,
                    args.node_selector_key,
                    args.node_selector_value,
                    args.image_pull_policy,
                    args.node_local_image_id,
                    args.tolerate_control_plane,
                    args.queue,
                ),
            )
        elif args.command == "render-trainjob":
            write_json(
                Path(args.output),
                render_image_trainjob(
                    args.name,
                    args.namespace,
                    args.image,
                    args.pvc,
                    args.config_path,
                    args.gpu_count,
                    args.queue,
                    args.runtime,
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

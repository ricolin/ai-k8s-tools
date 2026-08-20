from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.security_research import ContractError, _load_json, _require, _require_sha256
from ai_build_tools_k8s.workflow import sha256_file, write_json


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
    return {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


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
) -> dict[str, Any]:
    require_image_digest(image, "image")
    _require(mode in {"train", "generate"}, "unsupported image job mode")
    _require(gpu_count >= 1, "gpu_count must be positive")
    _require(config_path.startswith("/workspace/"), "config must be on the workspace PVC")
    script = "train_stage.py" if mode == "train" else "generate_comparison.py"
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "name": mode,
                "image": image,
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
                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
            }
        ],
        "volumes": [{"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}}],
    }
    if node_selector_key and node_selector_value:
        pod_spec["nodeSelector"] = {node_selector_key: node_selector_value}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec}},
    }


def create_release_manifest(
    name: str,
    base_digest: str,
    adapter_records: list[dict[str, Any]],
    prompt_digest: str,
    evaluation_digest: str,
    validation_level: str,
) -> dict[str, Any]:
    _require(name in {"release-a-watercolor", "release-b-watercolor-detail", "release-c-watercolor-complex"}, "invalid release name")
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
    job.add_argument("--output", required=True)
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
                ),
            )
    except ContractError as error:
        raise SystemExit(f"contract error: {error}") from error


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.image_model import (
    ContractError,
    render_image_job,
    render_image_trainjob,
)
from ai_build_tools_k8s.security_research import _require_sha256
from ai_build_tools_k8s.workflow import sha256_file, write_json


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid {description}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{description} must be a JSON object: {path}")
    return value


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} is required")
    _require_sha256(value, field)
    return value


def hydrate_training_parent(config_path: Path, parent_result_path: Path) -> dict[str, Any]:
    config = load_json_object(config_path, "image training config")
    parent = load_json_object(parent_result_path, "parent image training result")
    if config.get("stage") not in {"B-detail", "B-impressionism"}:
        raise ContractError(f"stage {config.get('stage')!r} cannot consume a parent result")
    if parent.get("state") != "COMPLETE" or parent.get("stage") != "A":
        raise ContractError("Release B requires a completed Release A parent")
    digest = _require_digest(parent.get("adapter_tree_digest"), "parent adapter tree digest")
    if not config.get("parent_adapter_path"):
        raise ContractError("Release B config does not contain a parent adapter path")
    configured = config.get("parent_adapter_digest")
    if configured is not None and configured != digest:
        raise ContractError("Release B config contains a different parent adapter digest")
    config["parent_adapter_digest"] = digest
    write_json(config_path, config)
    return config


def hydrate_generation_adapters(
    config_path: Path,
    adapter_result_paths: list[Path],
) -> dict[str, Any]:
    config = load_json_object(config_path, "image generation config")
    adapters = config.get("adapters", [])
    if not isinstance(adapters, list):
        raise ContractError("generation adapters must be a list")
    if len(adapters) != len(adapter_result_paths):
        raise ContractError("generation adapter count does not match supplied training results")
    for adapter, result_path in zip(adapters, adapter_result_paths, strict=True):
        if not isinstance(adapter, dict):
            raise ContractError("generation adapter must be an object")
        result = load_json_object(result_path, "image training result")
        if result.get("state") != "COMPLETE":
            raise ContractError("generation requires completed image training results")
        observed = _require_digest(result.get("adapter_digest"), "generation adapter digest")
        configured = adapter.get("digest")
        if configured not in {None, ""} and configured != observed:
            raise ContractError("generation config contains a different adapter digest")
        adapter["digest"] = observed
    write_json(config_path, config)
    return config


def validate_dependencies(paths: list[Path]) -> None:
    for path in paths:
        result = load_json_object(path, "image workflow dependency")
        if result.get("state") != "COMPLETE":
            raise ContractError(f"image workflow dependency is not complete: {path}")


def kubectl_json(*arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        ["kubectl", *arguments, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ContractError("kubectl did not return a JSON object")
    return value


def terminal_state(resource: dict[str, Any]) -> str | None:
    for condition in resource.get("status", {}).get("conditions", []):
        if condition.get("status") != "True":
            continue
        if condition.get("type") in {"Complete", "Succeeded"}:
            return "COMPLETE"
        if condition.get("type") == "Failed":
            return "FAILED"
    return None


def stage_evidence_dir(root: Path, stage: str) -> Path:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not stage or any(character not in allowed for character in stage):
        raise ContractError("evidence stage must be a lowercase name")
    return root / stage


def capture_namespace_resources(namespace: str, evidence_dir: Path) -> None:
    resources = {
        "trainjobs": "trainjobs.trainer.kubeflow.org",
        "kueue-workloads": "workloads.kueue.x-k8s.io",
        "jobsets": "jobsets.jobset.x-k8s.io",
        "jobs": "jobs.batch",
        "pods": "pods",
    }
    for filename, resource in resources.items():
        write_json(evidence_dir / f"{filename}.json", kubectl_json("get", resource, "-n", namespace))


def validate_training_result(path: Path, stage: str, gpu_count: int) -> dict[str, Any]:
    result = load_json_object(path, "image training result")
    if result.get("stage") != stage:
        raise ContractError(f"image training stage mismatch: expected {stage}, got {result.get('stage')}")
    if result.get("gpu_count") != gpu_count:
        raise ContractError(
            f"image training GPU count mismatch: expected {gpu_count}, got {result.get('gpu_count')}"
        )
    _require_digest(result.get("adapter_digest"), "image adapter digest")
    _require_digest(result.get("adapter_tree_digest"), "image adapter tree digest")
    return result


def run_workload(
    manifest: dict[str, Any],
    config: dict[str, Any],
    evidence_dir: Path,
    output: Path,
    timeout: int,
    gpu_count: int,
) -> dict[str, Any]:
    namespace = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    kind = manifest["kind"]
    resource = "trainjob" if kind == "TrainJob" else "job"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    write_json(evidence_dir / f"{resource}.rendered.json", manifest)
    subprocess.run(["kubectl", "create", "-f", str(evidence_dir / f"{resource}.rendered.json")], check=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = kubectl_json("get", resource, name, "-n", namespace)
        write_json(evidence_dir / f"{resource}.observed.json", observed)
        state = terminal_state(observed)
        if state is None:
            time.sleep(10)
            continue
        capture_namespace_resources(namespace, evidence_dir)
        summary: dict[str, Any] = {
            "schema_version": "1.0.0",
            "kind": f"{kind}Run",
            "name": name,
            "namespace": namespace,
            "state": state,
            "uid": observed["metadata"]["uid"],
            "queue": manifest["metadata"].get("labels", {}).get("kueue.x-k8s.io/queue-name"),
        }
        if state == "COMPLETE":
            output_dir = config.get("output_dir")
            if not isinstance(output_dir, str) or not output_dir.startswith("/workspace/"):
                raise ContractError("image output_dir must be on the workspace")
            if kind == "TrainJob":
                result = validate_training_result(
                    Path(output_dir) / "training-result.json",
                    str(config.get("stage")),
                    gpu_count,
                )
                summary.update(result)
                summary["runtime"] = manifest["spec"]["runtimeRef"]["name"]
            else:
                metadata = Path(output_dir) / "metadata.json"
                if not metadata.is_file():
                    raise ContractError("image generation metadata is missing")
                summary.update(
                    {
                        "release_name": config.get("release_name"),
                        "metadata_digest": f"sha256:{sha256_file(metadata)}",
                    }
                )
        write_json(output, summary)
        if state == "FAILED":
            raise ContractError(f"{kind} failed: {namespace}/{name}")
        return summary
    capture_namespace_resources(namespace, evidence_dir)
    raise ContractError(f"{kind} timed out after {timeout}s: {namespace}/{name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Kueue-managed image workflow stage")
    parser.add_argument("--mode", choices=("train", "generate"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--pvc", required=True)
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--gpu-count", type=int, required=True)
    parser.add_argument("--queue", default="ai-workflows")
    parser.add_argument("--runtime", default="torch-distributed")
    parser.add_argument("--node-selector-key", default="")
    parser.add_argument("--node-selector-value", default="")
    parser.add_argument("--image-pull-policy", choices=("IfNotPresent", "Never"), default="IfNotPresent")
    parser.add_argument("--node-local-image-id", default="")
    parser.add_argument("--tolerate-control-plane", action="store_true")
    parser.add_argument("--parent-result", type=Path)
    parser.add_argument("--adapter-result", action="append", type=Path, default=[])
    parser.add_argument("--dependency-result", action="append", type=Path, default=[])
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--evidence-stage", default="")
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        validate_dependencies(args.dependency_result)
        config_path = Path(args.config_path)
        if args.mode == "train":
            if args.adapter_result:
                raise ContractError("training does not accept --adapter-result")
            config = (
                hydrate_training_parent(config_path, args.parent_result)
                if args.parent_result
                else load_json_object(config_path, "image training config")
            )
            if not args.parent_result and config.get("parent_adapter_path"):
                raise ContractError("Release B training requires --parent-result")
            manifest = render_image_trainjob(
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
            )
        else:
            if args.parent_result:
                raise ContractError("generation does not accept --parent-result")
            config = hydrate_generation_adapters(config_path, args.adapter_result)
            manifest = render_image_job(
                args.name,
                args.namespace,
                args.image,
                args.pvc,
                args.config_path,
                args.gpu_count,
                "generate",
                args.node_selector_key,
                args.node_selector_value,
                args.image_pull_policy,
                args.node_local_image_id,
                args.tolerate_control_plane,
                args.queue,
            )
        evidence_dir = Path(args.evidence_dir)
        if args.evidence_stage:
            evidence_dir = stage_evidence_dir(evidence_dir, args.evidence_stage)
        run_workload(
            manifest,
            config,
            evidence_dir,
            Path(args.output),
            args.timeout,
            args.gpu_count,
        )
    except (ContractError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"image workload failed: {error}") from error


if __name__ == "__main__":
    main()

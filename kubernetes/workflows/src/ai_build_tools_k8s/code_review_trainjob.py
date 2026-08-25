from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.code_review_model import (
    ContractError,
    render_training_trainjob,
    require_sha256,
)
from ai_build_tools_k8s.workflow import write_json


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid {description}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{description} must be a JSON object: {path}")
    return value


def hydrate_parent_digest(config_path: Path, parent_result_path: Path) -> dict[str, Any]:
    config = load_json_object(config_path, "training config")
    parent_result = load_json_object(parent_result_path, "parent training result")
    stage = config.get("stage")
    expected_parent_stage = {"B": "A", "C": "B"}.get(stage)
    if expected_parent_stage is None:
        raise ContractError(f"stage {stage!r} cannot consume a parent result")
    if parent_result.get("state") != "COMPLETE":
        raise ContractError("parent TrainJob did not complete")
    if parent_result.get("stage") != expected_parent_stage:
        raise ContractError(
            f"parent stage mismatch: expected {expected_parent_stage}, "
            f"got {parent_result.get('stage')}"
        )
    parent_digest = parent_result.get("adapter_digest")
    if not isinstance(parent_digest, str):
        raise ContractError("parent training result does not contain an adapter digest")
    require_sha256(parent_digest, "parent adapter digest")
    if not config.get("parent_adapter_path"):
        raise ContractError("child training config does not contain a parent adapter path")
    configured_digest = config.get("parent_adapter_digest")
    if configured_digest is not None and configured_digest != parent_digest:
        raise ContractError("child training config contains a different parent adapter digest")
    config["parent_adapter_digest"] = parent_digest
    write_json(config_path, config)
    return config


def load_training_result(
    path: Path,
    expected_stage: str,
    expected_world_size: int,
) -> dict[str, Any]:
    result = load_json_object(path, "training result")
    if result.get("stage") != expected_stage:
        raise ContractError(
            f"training result stage mismatch: expected {expected_stage}, got {result.get('stage')}"
        )
    if result.get("world_size") != expected_world_size:
        raise ContractError(
            f"training result world size mismatch: expected {expected_world_size}, "
            f"got {result.get('world_size')}"
        )
    adapter_digest = result.get("adapter_digest")
    if not isinstance(adapter_digest, str):
        raise ContractError("training result does not contain an adapter digest")
    require_sha256(adapter_digest, "training result adapter digest")
    return result


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
    conditions = resource.get("status", {}).get("conditions", [])
    for condition in conditions:
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
        "kueue-workloads": "workloads.kueue.x-k8s.io",
        "jobsets": "jobsets.jobset.x-k8s.io",
        "jobs": "jobs.batch",
        "pods": "pods",
    }
    for filename, resource in resources.items():
        write_json(evidence_dir / f"{filename}.json", kubectl_json("get", resource, "-n", namespace))


def run_trainjob(
    manifest: dict[str, Any],
    config: dict[str, Any],
    evidence_dir: Path,
    output: Path,
    timeout: int,
) -> dict[str, Any]:
    namespace = manifest["metadata"]["namespace"]
    name = manifest["metadata"]["name"]
    evidence_dir.mkdir(parents=True, exist_ok=True)
    rendered = evidence_dir / "trainjob.rendered.json"
    write_json(rendered, manifest)
    subprocess.run(["kubectl", "create", "-f", str(rendered)], check=True)

    deadline = time.monotonic() + timeout
    observed: dict[str, Any] = {}
    while time.monotonic() < deadline:
        observed = kubectl_json("get", "trainjob", name, "-n", namespace)
        write_json(evidence_dir / "trainjob.observed.json", observed)
        state = terminal_state(observed)
        if state is not None:
            capture_namespace_resources(namespace, evidence_dir)
            training_result: dict[str, Any] | None = None
            if state == "COMPLETE":
                output_dir = config.get("output_dir")
                stage = config.get("stage")
                if not isinstance(output_dir, str) or not output_dir.startswith("/workspace/"):
                    raise ContractError("training config output_dir must be on the workspace")
                if not isinstance(stage, str) or not stage:
                    raise ContractError("training config stage is required")
                training_result_path = Path(output_dir) / "training-result.json"
                training_result = load_training_result(
                    training_result_path,
                    stage,
                    manifest["spec"]["trainer"]["numProcPerNode"],
                )
            summary = {
                "schema_version": "1.0.0",
                "kind": "TrainJobRun",
                "name": name,
                "namespace": namespace,
                "state": state,
                "uid": observed["metadata"]["uid"],
                "queue": manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"],
                "runtime": manifest["spec"]["runtimeRef"]["name"],
                "stage": config["stage"],
            }
            if training_result is not None:
                summary.update(training_result)
            write_json(output, summary)
            if state == "FAILED":
                raise ContractError(f"TrainJob failed: {namespace}/{name}")
            return summary
        time.sleep(10)

    capture_namespace_resources(namespace, evidence_dir)
    raise ContractError(f"TrainJob timed out after {timeout}s: {namespace}/{name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one queued code-review Kubeflow TrainJob")
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--trainer-image", required=True)
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
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--evidence-stage", default="")
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        config_path = Path(args.config_path)
        config = (
            hydrate_parent_digest(config_path, args.parent_result)
            if args.parent_result
            else load_json_object(config_path, "training config")
        )
        if not args.parent_result and (
            config.get("parent_adapter_path") or config.get("parent_adapter_digest")
        ):
            raise ContractError("a child training config requires --parent-result")
        manifest = render_training_trainjob(
            args.name,
            args.namespace,
            args.trainer_image,
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
        evidence_dir = Path(args.evidence_dir)
        if args.evidence_stage:
            evidence_dir = stage_evidence_dir(evidence_dir, args.evidence_stage)
        run_trainjob(
            manifest,
            config,
            evidence_dir,
            Path(args.output),
            args.timeout,
        )
    except (ContractError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"TrainJob run failed: {error}") from error


if __name__ == "__main__":
    main()

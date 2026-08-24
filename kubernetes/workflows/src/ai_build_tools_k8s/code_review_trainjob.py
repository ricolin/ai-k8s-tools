from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_build_tools_k8s.code_review_model import ContractError, render_training_trainjob
from ai_build_tools_k8s.workflow import write_json


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
            summary = {
                "schema_version": "1.0.0",
                "kind": "TrainJobRun",
                "name": name,
                "namespace": namespace,
                "state": state,
                "uid": observed["metadata"]["uid"],
                "queue": manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"],
                "runtime": manifest["spec"]["runtimeRef"]["name"],
            }
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
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
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
        run_trainjob(manifest, Path(args.evidence_dir), Path(args.output), args.timeout)
    except (ContractError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"TrainJob run failed: {error}") from error


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from kfp import Client
from model_registry import ModelRegistry

from ai_build_tools_k8s.workflow import normalize_http_endpoint


def wait_for_run(client: Client, run_id: str, timeout: int) -> Any:
    run = client.wait_for_run_completion(run_id=run_id, timeout=timeout, sleep_duration=10)
    state = str(run.state).upper()
    if "SUCCEEDED" not in state:
        raise SystemExit(f"Kubeflow run {run_id} ended in state {run.state}")
    return run


def start_run(client: Client, package: Path, name: str, arguments: dict[str, Any], timeout: int) -> str:
    run = client.create_run_from_pipeline_package(
        pipeline_file=str(package),
        arguments=arguments,
        run_name=name,
        enable_caching=False,
    )
    run_id = run.run_id
    wait_for_run(client, run_id, timeout)
    return run_id


def model_info(registry: ModelRegistry, model_name: str, model_version: str) -> dict[str, Any]:
    version = registry.get_model_version(model_name, model_version)
    artifact = registry.get_model_artifact(model_name, model_version)
    if version is None or artifact is None:
        raise SystemExit(f"Hub did not return {model_name}/{model_version}")
    return {
        "version": model_version,
        "version_id": version.id,
        "properties": dict(version.custom_properties or {}),
        "artifact_uri": artifact.uri,
        "artifact_id": artifact.id,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kfp-host", default="http://127.0.0.1:8888")
    parser.add_argument("--registry-host", default="127.0.0.1")
    parser.add_argument("--registry-port", type=int, default=8081)
    parser.add_argument("--train-pipeline", type=Path, required=True)
    parser.add_argument("--deploy-pipeline", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--profile", default="kubernetes-fixture")
    parser.add_argument("--evidence-class", default="kubernetes-fixture")
    parser.add_argument("--evidence-level", default="mechanics")
    parser.add_argument("--author", default="ai-build-tools")
    parser.add_argument("--workload-namespace", default="ai-workflows")
    parser.add_argument(
        "--registry-service-host",
        default="model-registry-service.kubeflow.svc.cluster.local",
    )
    parser.add_argument("--registry-service-port", type=int, default=8080)
    parser.add_argument("--model-name", default="cute-bear-mechanics")
    parser.add_argument("--base-model-ref", default="stabilityai/stable-diffusion-xl-base-1.0")
    parser.add_argument("--base-model-revision", default="462165984030d82259a11f4367a4eed129e94a7b")
    parser.add_argument("--dataset-digest", default="sha256:90d2163ca1f418ae272fa597de8ed1b257a301d36dc9c20855c907fb366d9f2a")
    args = parser.parse_args()

    client = Client(host=args.kfp_host)
    registry = ModelRegistry(
        server_address=normalize_http_endpoint(args.registry_host),
        port=args.registry_port,
        author=args.author,
        is_secure=False,
    )
    version_a = f"model-a-{args.run_label}"
    version_b = f"model-b-{args.run_label}"
    evidence: dict[str, Any] = {
        "schema_version": "0.1.0",
        "run_label": args.run_label,
        "started_unix": int(time.time()),
        "evidence_class": args.evidence_class,
        "evidence_level": args.evidence_level,
        "runs": {},
        "models": {},
    }

    common = {
        "model_name": args.model_name,
        "base_model_ref": args.base_model_ref,
        "base_model_revision": args.base_model_revision,
        "dataset_digest": args.dataset_digest,
        "profile": args.profile,
        "evidence_class": args.evidence_class,
        "evidence_level": args.evidence_level,
        "pilot_steps": 4,
        "training_steps": 12,
        "rank": 4,
        "seed": 26081001,
        "expected_images": 3,
        "registry_host": args.registry_service_host,
        "registry_port": args.registry_service_port,
    }
    train_a = start_run(
        client,
        args.train_pipeline,
        f"train-a-{args.run_label}",
        {**common, "model_version": version_a, "run_id": f"train-a-{args.run_label}"},
        args.timeout,
    )
    evidence["runs"]["train_a"] = train_a
    model_a = model_info(registry, args.model_name, version_a)
    evidence["models"]["a_candidate"] = model_a

    deploy_a = start_run(
        client,
        args.deploy_pipeline,
        f"deploy-a-{args.run_label}",
        {
            "model_name": args.model_name,
            "model_version": version_a,
            "service_name": f"cute-bear-a-{args.run_label}",
            "base_uri": model_a["properties"]["base_artifact_uri"],
            "adapter_uri": model_a["artifact_uri"],
            "runtime_image": args.runtime_image,
            "namespace": args.workload_namespace,
            "registry_host": args.registry_service_host,
            "registry_port": args.registry_service_port,
        },
        args.timeout,
    )
    evidence["runs"]["deploy_a"] = deploy_a
    model_a = model_info(registry, args.model_name, version_a)
    if model_a["properties"].get("lifecycle_status") != "RELEASED":
        raise SystemExit("model A was not promoted to RELEASED")
    evidence["models"]["a_released"] = model_a

    train_b = start_run(
        client,
        args.train_pipeline,
        f"train-b-{args.run_label}",
        {
            **common,
            "model_version": version_b,
            "run_id": f"train-b-{args.run_label}",
            "parent_uri": model_a["artifact_uri"],
            "parent_digest": f"sha256:{model_a['properties']['adapter_sha256']}",
            "parent_model_version": version_a,
            "seed": 26081002,
        },
        args.timeout,
    )
    evidence["runs"]["train_b"] = train_b
    model_b = model_info(registry, args.model_name, version_b)
    if model_b["properties"].get("parent_model_version") != version_a:
        raise SystemExit("model B does not record model A as its parent")
    evidence["models"]["b_candidate"] = model_b

    deploy_b = start_run(
        client,
        args.deploy_pipeline,
        f"deploy-b-{args.run_label}",
        {
            "model_name": args.model_name,
            "model_version": version_b,
            "service_name": f"cute-bear-b-{args.run_label}",
            "base_uri": model_b["properties"]["base_artifact_uri"],
            "adapter_uri": model_b["artifact_uri"],
            "runtime_image": args.runtime_image,
            "namespace": args.workload_namespace,
            "registry_host": args.registry_service_host,
            "registry_port": args.registry_service_port,
        },
        args.timeout,
    )
    evidence["runs"]["deploy_b"] = deploy_b
    model_b = model_info(registry, args.model_name, version_b)
    if model_b["properties"].get("lifecycle_status") != "RELEASED":
        raise SystemExit("model B was not promoted to RELEASED")
    evidence["models"]["b_released"] = model_b
    evidence["completed_unix"] = int(time.time())
    evidence["status"] = "PASS"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

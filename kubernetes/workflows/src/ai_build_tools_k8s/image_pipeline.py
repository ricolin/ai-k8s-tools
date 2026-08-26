import argparse
import json
from pathlib import Path
from typing import Any

from kfp import Client, compiler, dsl, kubernetes


RUN_ARGUMENTS = {
    "trainjob_a_name",
    "trainjob_b_name",
    "gallery_foundation_name",
    "gallery_a_name",
    "gallery_b_name",
    "workload_image",
    "workload_image_id",
    "pvc_name",
    "config_a_path",
    "config_b_path",
    "config_gallery_foundation_path",
    "config_gallery_a_path",
    "config_gallery_b_path",
    "evidence_root",
    "workload_namespace",
    "training_gpu_count",
}


def make_pipeline(workflow_image: str):
    def common_args(
        mode: str,
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        gpu_count: Any,
        evidence_root: str,
        evidence_stage: str,
        workload_namespace: str,
    ) -> list[Any]:
        return [
            "--mode",
            mode,
            "--name",
            name,
            "--namespace",
            workload_namespace,
            "--image",
            workload_image,
            "--pvc",
            pvc_name,
            "--config-path",
            config_path,
            "--gpu-count",
            gpu_count,
            "--queue",
            "ai-workflows",
            "--runtime",
            "torch-distributed",
            "--node-selector-key",
            "nvidia.com/gpu.product",
            "--node-selector-value",
            "NVIDIA-H200",
            "--image-pull-policy",
            "Never",
            "--node-local-image-id",
            workload_image_id,
            "--tolerate-control-plane",
            "--timeout",
            "14400",
            "--evidence-dir",
            evidence_root,
            "--evidence-stage",
            evidence_stage,
        ]

    @dsl.container_component
    def root_train_component(
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        gpu_count: int,
        evidence_root: str,
        workload_namespace: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-image-workload"],
            args=common_args(
                "train",
                name,
                workload_image,
                workload_image_id,
                pvc_name,
                config_path,
                gpu_count,
                evidence_root,
                "release-a",
                workload_namespace,
            )
            + ["--output", result.path],
        )

    @dsl.container_component
    def child_train_component(
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        gpu_count: int,
        parent_result: dsl.Input[dsl.Artifact],
        evidence_root: str,
        workload_namespace: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-image-workload"],
            args=common_args(
                "train",
                name,
                workload_image,
                workload_image_id,
                pvc_name,
                config_path,
                gpu_count,
                evidence_root,
                "release-b",
                workload_namespace,
            )
            + ["--parent-result", parent_result.path, "--output", result.path],
        )

    @dsl.container_component
    def foundation_gallery_component(
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        dependency_result: dsl.Input[dsl.Artifact],
        evidence_root: str,
        workload_namespace: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-image-workload"],
            args=common_args(
                "generate",
                name,
                workload_image,
                workload_image_id,
                pvc_name,
                config_path,
                "1",
                evidence_root,
                "gallery-foundation",
                workload_namespace,
            )
            + ["--dependency-result", dependency_result.path, "--output", result.path],
        )

    @dsl.container_component
    def release_a_gallery_component(
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        dependency_result: dsl.Input[dsl.Artifact],
        adapter_result: dsl.Input[dsl.Artifact],
        evidence_root: str,
        workload_namespace: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-image-workload"],
            args=common_args(
                "generate",
                name,
                workload_image,
                workload_image_id,
                pvc_name,
                config_path,
                "1",
                evidence_root,
                "gallery-a",
                workload_namespace,
            )
            + [
                "--dependency-result",
                dependency_result.path,
                "--adapter-result",
                adapter_result.path,
                "--output",
                result.path,
            ],
        )

    @dsl.container_component
    def release_b_gallery_component(
        name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_path: str,
        dependency_result: dsl.Input[dsl.Artifact],
        adapter_a_result: dsl.Input[dsl.Artifact],
        adapter_b_result: dsl.Input[dsl.Artifact],
        evidence_root: str,
        workload_namespace: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-image-workload"],
            args=common_args(
                "generate",
                name,
                workload_image,
                workload_image_id,
                pvc_name,
                config_path,
                "1",
                evidence_root,
                "gallery-b",
                workload_namespace,
            )
            + [
                "--dependency-result",
                dependency_result.path,
                "--adapter-result",
                adapter_a_result.path,
                "--adapter-result",
                adapter_b_result.path,
                "--output",
                result.path,
            ],
        )

    @dsl.pipeline(name="h200-image-trainjob-ab-galleries")
    def pipeline(
        trainjob_a_name: str,
        trainjob_b_name: str,
        gallery_foundation_name: str,
        gallery_a_name: str,
        gallery_b_name: str,
        workload_image: str,
        workload_image_id: str,
        pvc_name: str,
        config_a_path: str,
        config_b_path: str,
        config_gallery_foundation_path: str,
        config_gallery_a_path: str,
        config_gallery_b_path: str,
        evidence_root: str,
        workload_namespace: str,
        training_gpu_count: int = 7,
    ) -> None:
        stage_a = root_train_component(
            name=trainjob_a_name,
            workload_image=workload_image,
            workload_image_id=workload_image_id,
            pvc_name=pvc_name,
            config_path=config_a_path,
            gpu_count=training_gpu_count,
            evidence_root=evidence_root,
            workload_namespace=workload_namespace,
        )
        stage_b = child_train_component(
            name=trainjob_b_name,
            workload_image=workload_image,
            workload_image_id=workload_image_id,
            pvc_name=pvc_name,
            config_path=config_b_path,
            gpu_count=training_gpu_count,
            parent_result=stage_a.outputs["result"],
            evidence_root=evidence_root,
            workload_namespace=workload_namespace,
        )
        foundation = foundation_gallery_component(
            name=gallery_foundation_name,
            workload_image=workload_image,
            workload_image_id=workload_image_id,
            pvc_name=pvc_name,
            config_path=config_gallery_foundation_path,
            dependency_result=stage_b.outputs["result"],
            evidence_root=evidence_root,
            workload_namespace=workload_namespace,
        )
        gallery_a = release_a_gallery_component(
            name=gallery_a_name,
            workload_image=workload_image,
            workload_image_id=workload_image_id,
            pvc_name=pvc_name,
            config_path=config_gallery_a_path,
            dependency_result=foundation.outputs["result"],
            adapter_result=stage_a.outputs["result"],
            evidence_root=evidence_root,
            workload_namespace=workload_namespace,
        )
        gallery_b = release_b_gallery_component(
            name=gallery_b_name,
            workload_image=workload_image,
            workload_image_id=workload_image_id,
            pvc_name=pvc_name,
            config_path=config_gallery_b_path,
            dependency_result=gallery_a.outputs["result"],
            adapter_a_result=stage_a.outputs["result"],
            adapter_b_result=stage_b.outputs["result"],
            evidence_root=evidence_root,
            workload_namespace=workload_namespace,
        )

        for task in (stage_a, stage_b, foundation, gallery_a, gallery_b):
            kubernetes.set_image_pull_policy(task, "Never")
            kubernetes.set_security_context(task, run_as_non_root=True)
            kubernetes.mount_pvc(task, pvc_name=pvc_name, mount_path="/workspace")
            kubernetes.add_node_selector(task, "nvidia.com/gpu.product", "NVIDIA-H200")
            kubernetes.add_toleration(
                task,
                key="node-role.kubernetes.io/control-plane",
                operator="Exists",
                effect="NoSchedule",
            )
            kubernetes.add_toleration(
                task,
                key="node-role.kubernetes.io/master",
                operator="Exists",
                effect="NoSchedule",
            )
            kubernetes.set_timeout(task, 15000)

    return pipeline


def load_run_arguments(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("pipeline arguments must be a JSON object")
    missing = RUN_ARGUMENTS - value.keys()
    extra = value.keys() - RUN_ARGUMENTS
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unknown: {', '.join(sorted(extra))}")
        raise ValueError("invalid pipeline arguments (" + "; ".join(details) + ")")
    string_arguments = RUN_ARGUMENTS - {"training_gpu_count"}
    if not all(isinstance(value[key], str) and value[key] for key in string_arguments):
        raise ValueError("every string pipeline argument must be non-empty")
    if not isinstance(value["training_gpu_count"], int) or not 1 <= value["training_gpu_count"] <= 7:
        raise ValueError("training_gpu_count must be an integer from 1 through 7")
    return value


def submit_run(
    host: str,
    package: Path,
    run_name: str,
    arguments: dict[str, Any],
    namespace: str,
    service_account: str,
    timeout: int,
) -> dict[str, Any]:
    if arguments.get("workload_namespace") != namespace:
        raise ValueError(
            "workload_namespace must equal the KFP run namespace because "
            "pipeline task Pods and their PVC are namespace-scoped"
        )
    client = Client(host=host, namespace=namespace)
    run = client.create_run_from_pipeline_package(
        pipeline_file=str(package),
        arguments=arguments,
        run_name=run_name,
        namespace=namespace,
        service_account=service_account,
        enable_caching=False,
    )
    observed = client.wait_for_run_completion(
        run_id=run.run_id,
        timeout=timeout,
        sleep_duration=10,
    )
    state = str(observed.state).upper()
    result = {
        "schema_version": "1.0.0",
        "run_id": run.run_id,
        "run_name": run_name,
        "state": state,
        "namespace": namespace,
        "service_account": service_account,
    }
    if "SUCCEEDED" not in state:
        raise RuntimeError(f"Kubeflow run {run.run_id} ended in state {observed.state}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile the H200 image A/B TrainJob and gallery pipeline")
    parser.add_argument("--workflow-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kfp-host", default="")
    parser.add_argument("--run-name", default="")
    parser.add_argument("--arguments", type=Path)
    parser.add_argument("--run-output", type=Path)
    parser.add_argument("--namespace", default="kubeflow")
    parser.add_argument("--service-account", default="ai-workflow-runner")
    parser.add_argument("--timeout", type=int, default=45000)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    compiler.Compiler().compile(make_pipeline(args.workflow_image), str(args.output))
    submit_values = (args.kfp_host, args.run_name, args.arguments, args.run_output)
    if any(submit_values) and not all(submit_values):
        parser.error("--kfp-host, --run-name, --arguments, and --run-output are required together")
    if args.kfp_host:
        result = submit_run(
            args.kfp_host,
            args.output,
            args.run_name,
            load_run_arguments(args.arguments),
            args.namespace,
            args.service_account,
            args.timeout,
        )
        args.run_output.parent.mkdir(parents=True, exist_ok=True)
        args.run_output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

import argparse
from pathlib import Path

from kfp import compiler, dsl, kubernetes


def create_components(workflow_image: str):
    @dsl.container_component
    def resolve_component(
        base_model_ref: str,
        base_model_revision: str,
        dataset_digest: str,
        parent_uri: str,
        parent_digest: str,
        profile: str,
        evidence_class: str,
        evidence_level: str,
        resolved: dsl.Output[dsl.Artifact],
        parent: dsl.Output[dsl.Model],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "resolve",
                "--base-model-ref",
                base_model_ref,
                "--base-model-revision",
                base_model_revision,
                "--dataset-digest",
                dataset_digest,
                "--parent-uri",
                parent_uri,
                "--parent-digest",
                parent_digest,
                "--profile",
                profile,
                "--evidence-class",
                evidence_class,
                "--evidence-level",
                evidence_level,
                "--output",
                resolved.path,
                "--parent-output",
                parent.path,
            ],
        )

    @dsl.container_component
    def train_component(
        parent: dsl.Input[dsl.Model],
        dataset_digest: str,
        steps: int,
        rank: int,
        seed: int,
        run_id: str,
        adapter: dsl.Output[dsl.Model],
        metrics: dsl.Output[dsl.Metrics],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "train-fixture",
                "--parent",
                parent.path,
                "--adapter",
                adapter.path,
                "--metrics",
                metrics.path,
                "--dataset-digest",
                dataset_digest,
                "--steps",
                steps,
                "--rank",
                rank,
                "--seed",
                seed,
                "--run-id",
                run_id,
            ],
        )

    @dsl.container_component
    def generate_component(
        adapter: dsl.Input[dsl.Model],
        prompt: str,
        seed: int,
        count: int,
        images: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "generate-fixture",
                "--adapter",
                adapter.path,
                "--output",
                images.path,
                "--prompt",
                prompt,
                "--seed",
                seed,
                "--count",
                count,
            ],
        )

    @dsl.container_component
    def evaluate_component(
        adapter: dsl.Input[dsl.Model],
        images: dsl.Input[dsl.Artifact],
        expected_images: int,
        report: dsl.Output[dsl.Metrics],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "evaluate-fixture",
                "--adapter",
                adapter.path,
                "--images",
                images.path,
                "--output",
                report.path,
                "--expected-images",
                expected_images,
            ],
        )

    @dsl.container_component
    def register_component(
        adapter: dsl.Input[dsl.Model],
        resolved: dsl.Input[dsl.Artifact],
        evaluation: dsl.Input[dsl.Metrics],
        model_name: str,
        model_version: str,
        parent_model_version: str,
        base_model_ref: str,
        base_model_revision: str,
        dataset_digest: str,
        run_id: str,
        evidence_class: str,
        evidence_level: str,
        registry_host: str,
        registry_port: int,
        candidate: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "register-candidate",
                "--adapter",
                adapter.path,
                "--adapter-uri",
                adapter.uri,
                "--base-artifact-uri",
                resolved.uri,
                "--evaluation",
                evaluation.path,
                "--output",
                candidate.path,
                "--model-name",
                model_name,
                "--model-version",
                model_version,
                "--parent-model-version",
                parent_model_version,
                "--base-model-ref",
                base_model_ref,
                "--base-model-revision",
                base_model_revision,
                "--dataset-digest",
                dataset_digest,
                "--run-id",
                run_id,
                "--evidence-class",
                evidence_class,
                "--evidence-level",
                evidence_level,
                "--registry-host",
                registry_host,
                "--registry-port",
                registry_port,
            ],
        )

    @dsl.container_component
    def deploy_component(
        service_name: str,
        namespace: str,
        service_account: str,
        base_uri: str,
        adapter_uri: str,
        runtime_image: str,
        evidence_class: str,
        deployment: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "deploy",
                "--service-name",
                service_name,
                "--namespace",
                namespace,
                "--service-account",
                service_account,
                "--base-uri",
                base_uri,
                "--adapter-uri",
                adapter_uri,
                "--runtime-image",
                runtime_image,
                "--evidence-class",
                evidence_class,
                "--output",
                deployment.path,
            ],
        )

    @dsl.container_component
    def verify_component(
        deployment: dsl.Input[dsl.Artifact],
        service_name: str,
        namespace: str,
        prompt: str,
        seed: int,
        verification: dsl.Output[dsl.Metrics],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "verify",
                "--service-name",
                service_name,
                "--namespace",
                namespace,
                "--prompt",
                prompt,
                "--seed",
                seed,
                "--output",
                verification.path,
            ],
        )

    @dsl.container_component
    def promote_component(
        verification: dsl.Input[dsl.Metrics],
        model_name: str,
        model_version: str,
        service_name: str,
        namespace: str,
        registry_host: str,
        registry_port: int,
        release: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-k8s-workflow"],
            args=[
                "promote",
                "--verification",
                verification.path,
                "--output",
                release.path,
                "--model-name",
                model_name,
                "--model-version",
                model_version,
                "--service-name",
                service_name,
                "--namespace",
                namespace,
                "--registry-host",
                registry_host,
                "--registry-port",
                registry_port,
            ],
        )

    return {
        "resolve": resolve_component,
        "train": train_component,
        "generate": generate_component,
        "evaluate": evaluate_component,
        "register": register_component,
        "deploy": deploy_component,
        "verify": verify_component,
        "promote": promote_component,
    }


def add_placement(task: dsl.PipelineTask, key: str, value: str) -> None:
    if key and value:
        kubernetes.add_node_selector(task, key, value)


def make_training_pipeline(
    workflow_image: str,
    node_selector_key: str,
    node_selector_value: str,
    s3_endpoint_url: str,
):
    components = create_components(workflow_image)

    @dsl.pipeline(name="sdxl-lora-train-and-register")
    def pipeline(
        model_name: str,
        model_version: str,
        run_id: str,
        base_model_ref: str,
        base_model_revision: str,
        dataset_digest: str,
        parent_uri: str = "",
        parent_digest: str = "",
        parent_model_version: str = "",
        profile: str = "kubernetes-fixture",
        evidence_class: str = "kubernetes-fixture",
        evidence_level: str = "mechanics",
        pilot_steps: int = 4,
        training_steps: int = 12,
        rank: int = 4,
        seed: int = 26081001,
        prompt: str = "a photograph of a cbear cute little brown bear",
        expected_images: int = 3,
        registry_host: str = "model-registry-service.kubeflow.svc.cluster.local",
        registry_port: int = 8080,
    ) -> None:
        resolved = components["resolve"](
            base_model_ref=base_model_ref,
            base_model_revision=base_model_revision,
            dataset_digest=dataset_digest,
            parent_uri=parent_uri,
            parent_digest=parent_digest,
            profile=profile,
            evidence_class=evidence_class,
            evidence_level=evidence_level,
        )
        resolved.set_env_variable("AWS_ENDPOINT_URL", s3_endpoint_url)
        resolved.set_env_variable("AWS_DEFAULT_REGION", "us-east-1")
        kubernetes.use_secret_as_env(
            resolved,
            secret_name="mlpipeline-minio-artifact",
            secret_key_to_env={
                "accesskey": "AWS_ACCESS_KEY_ID",
                "secretkey": "AWS_SECRET_ACCESS_KEY",
            },
        )
        add_placement(resolved, node_selector_key, node_selector_value)

        pilot = components["train"](
            parent=resolved.outputs["parent"],
            dataset_digest=dataset_digest,
            steps=pilot_steps,
            rank=rank,
            seed=seed,
            run_id=run_id,
        )
        pilot.set_caching_options(False)
        add_placement(pilot, node_selector_key, node_selector_value)

        trained = components["train"](
            parent=resolved.outputs["parent"],
            dataset_digest=dataset_digest,
            steps=training_steps,
            rank=rank,
            seed=seed,
            run_id=run_id,
        ).after(pilot)
        trained.set_caching_options(False)
        add_placement(trained, node_selector_key, node_selector_value)

        generated = components["generate"](
            adapter=trained.outputs["adapter"],
            prompt=prompt,
            seed=seed,
            count=expected_images,
        )
        generated.set_caching_options(False)
        add_placement(generated, node_selector_key, node_selector_value)

        evaluated = components["evaluate"](
            adapter=trained.outputs["adapter"],
            images=generated.outputs["images"],
            expected_images=expected_images,
        )
        evaluated.set_caching_options(False)
        add_placement(evaluated, node_selector_key, node_selector_value)

        registered = components["register"](
            adapter=trained.outputs["adapter"],
            resolved=resolved.outputs["resolved"],
            evaluation=evaluated.outputs["report"],
            model_name=model_name,
            model_version=model_version,
            parent_model_version=parent_model_version,
            base_model_ref=base_model_ref,
            base_model_revision=base_model_revision,
            dataset_digest=dataset_digest,
            run_id=run_id,
            evidence_class=evidence_class,
            evidence_level=evidence_level,
            registry_host=registry_host,
            registry_port=registry_port,
        )
        registered.set_caching_options(False)
        add_placement(registered, node_selector_key, node_selector_value)

    return pipeline


def make_deployment_pipeline(workflow_image: str, node_selector_key: str, node_selector_value: str):
    components = create_components(workflow_image)

    @dsl.pipeline(name="sdxl-lora-deploy-verify-release")
    def pipeline(
        model_name: str,
        model_version: str,
        service_name: str,
        base_uri: str,
        adapter_uri: str,
        runtime_image: str,
        namespace: str = "kubeflow",
        service_account: str = "ai-build-tools-serving",
        evidence_class: str = "kubernetes-fixture",
        prompt: str = "a photograph of a cbear cute little brown bear",
        seed: int = 26081001,
        registry_host: str = "model-registry-service.kubeflow.svc.cluster.local",
        registry_port: int = 8080,
    ) -> None:
        deployed = components["deploy"](
            service_name=service_name,
            namespace=namespace,
            service_account=service_account,
            base_uri=base_uri,
            adapter_uri=adapter_uri,
            runtime_image=runtime_image,
            evidence_class=evidence_class,
        )
        deployed.set_caching_options(False)
        add_placement(deployed, node_selector_key, node_selector_value)

        verified = components["verify"](
            deployment=deployed.outputs["deployment"],
            service_name=service_name,
            namespace=namespace,
            prompt=prompt,
            seed=seed,
        )
        verified.set_caching_options(False)
        add_placement(verified, node_selector_key, node_selector_value)

        promoted = components["promote"](
            verification=verified.outputs["verification"],
            model_name=model_name,
            model_version=model_version,
            service_name=service_name,
            namespace=namespace,
            registry_host=registry_host,
            registry_port=registry_port,
        )
        promoted.set_caching_options(False)
        add_placement(promoted, node_selector_key, node_selector_value)

    return pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-image", required=True)
    parser.add_argument("--node-selector-key", default="")
    parser.add_argument("--node-selector-value", default="")
    parser.add_argument("--s3-endpoint-url", default="http://seaweedfs.kubeflow.svc.cluster.local:9000")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    compiler.Compiler().compile(
        make_training_pipeline(
            args.workflow_image,
            args.node_selector_key,
            args.node_selector_value,
            args.s3_endpoint_url,
        ),
        str(args.output_dir / "sdxl-lora-train-and-register.yaml"),
    )
    compiler.Compiler().compile(
        make_deployment_pipeline(args.workflow_image, args.node_selector_key, args.node_selector_value),
        str(args.output_dir / "sdxl-lora-deploy-verify-release.yaml"),
    )


if __name__ == "__main__":
    main()

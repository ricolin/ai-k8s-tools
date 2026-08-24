import argparse
from pathlib import Path

from kfp import compiler, dsl, kubernetes


def make_pipeline(workflow_image: str):
    @dsl.container_component
    def trainjob_component(
        name: str,
        trainer_image: str,
        trainer_image_id: str,
        pvc_name: str,
        config_path: str,
        evidence_root: str,
        stage: str,
        result: dsl.Output[dsl.Artifact],
    ) -> dsl.ContainerSpec:
        return dsl.ContainerSpec(
            image=workflow_image,
            command=["ai-code-review-trainjob"],
            args=[
                "--name",
                name,
                "--namespace",
                "ai-workflows",
                "--trainer-image",
                trainer_image,
                "--pvc",
                pvc_name,
                "--config-path",
                config_path,
                "--gpu-count",
                "8",
                "--queue",
                "ai-workflows",
                "--runtime",
                "torch-distributed",
                "--node-selector-key",
                "ai-build-tools.ricolin.dev/accelerator",
                "--node-selector-value",
                "nvidia-h200",
                "--image-pull-policy",
                "Never",
                "--node-local-image-id",
                trainer_image_id,
                "--tolerate-control-plane",
                "--timeout",
                "14400",
                "--evidence-dir",
                dsl.ConcatPlaceholder([evidence_root, "/", stage]),
                "--output",
                result.path,
            ],
        )

    @dsl.pipeline(name="h200-code-review-trainjob-abc")
    def pipeline(
        trainjob_a_name: str,
        trainjob_b_name: str,
        trainjob_c_name: str,
        trainer_image: str,
        trainer_image_id: str,
        pvc_name: str,
        config_a_path: str,
        config_b_path: str,
        config_c_path: str,
        evidence_root: str,
    ) -> None:
        stage_a = trainjob_component(
            name=trainjob_a_name,
            trainer_image=trainer_image,
            trainer_image_id=trainer_image_id,
            pvc_name=pvc_name,
            config_path=config_a_path,
            evidence_root=evidence_root,
            stage="release-a",
        )
        stage_b = trainjob_component(
            name=trainjob_b_name,
            trainer_image=trainer_image,
            trainer_image_id=trainer_image_id,
            pvc_name=pvc_name,
            config_path=config_b_path,
            evidence_root=evidence_root,
            stage="release-b",
        ).after(stage_a)
        stage_c = trainjob_component(
            name=trainjob_c_name,
            trainer_image=trainer_image,
            trainer_image_id=trainer_image_id,
            pvc_name=pvc_name,
            config_path=config_c_path,
            evidence_root=evidence_root,
            stage="release-c",
        ).after(stage_b)

        for task in (stage_a, stage_b, stage_c):
            kubernetes.set_image_pull_policy(task, "Never")
            kubernetes.mount_pvc(task, pvc_name=pvc_name, mount_path="/workspace")
            kubernetes.add_node_selector(
                task,
                "ai-build-tools.ricolin.dev/accelerator",
                "nvidia-h200",
            )
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile the H200 code-review A/B/C TrainJob pipeline")
    parser.add_argument("--workflow-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    compiler.Compiler().compile(make_pipeline(args.workflow_image), str(args.output))


if __name__ == "__main__":
    main()

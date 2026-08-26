from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from kfp import compiler

import ai_build_tools_k8s.image_pipeline as pipeline_module
from ai_build_tools_k8s.image_pipeline import load_run_arguments, make_pipeline, submit_run


def arguments() -> dict:
    return {
        "trainjob_a_name": "image-a",
        "trainjob_b_name": "image-b",
        "gallery_foundation_name": "gallery-foundation",
        "gallery_a_name": "gallery-a",
        "gallery_b_name": "gallery-b",
        "workload_image": "image:v1",
        "workload_image_id": "sha256:" + "a" * 64,
        "pvc_name": "workspace",
        "config_a_path": "/workspace/a.json",
        "config_b_path": "/workspace/b.json",
        "config_gallery_foundation_path": "/workspace/foundation.json",
        "config_gallery_a_path": "/workspace/gallery-a.json",
        "config_gallery_b_path": "/workspace/gallery-b.json",
        "evidence_root": "/workspace/evidence",
        "workload_namespace": "kubeflow",
        "training_gpu_count": 7,
    }


def test_pipeline_owns_trainjobs_and_queued_galleries_without_gpu_tasks(tmp_path: Path) -> None:
    output = tmp_path / "pipeline.yaml"
    compiler.Compiler().compile(make_pipeline("workflow:v1"), str(output))
    rendered = output.read_text()
    assert rendered.count("ai-image-workload") == 5
    assert rendered.count("--mode") == 5
    assert rendered.count("--parent-result") == 1
    assert rendered.count("--adapter-result") == 3
    assert "training_gpu_count" in rendered
    assert "torch-distributed" in rendered
    assert 'key: nvidia.com/gpu' not in rendered
    platform = next(
        document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict) and "platforms" in document
    )
    executors = platform["platforms"]["kubernetes"]["deploymentSpec"]["executors"]
    assert len(executors) == 5
    assert all(executor["securityContext"]["runAsNonRoot"] is True for executor in executors.values())


def test_pipeline_arguments_are_exact_and_respect_the_serving_reserve(tmp_path: Path) -> None:
    path = tmp_path / "arguments.json"
    path.write_text(json.dumps(arguments()))
    assert load_run_arguments(path)["training_gpu_count"] == 7

    value = arguments()
    value["training_gpu_count"] = 8
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="1 through 7"):
        load_run_arguments(path)

    value = arguments()
    del value["gallery_b_name"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="missing"):
        load_run_arguments(path)


def test_submission_uses_run_namespace_and_service_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            observed["client"] = kwargs

        def create_run_from_pipeline_package(self, **kwargs: object) -> object:
            observed["create"] = kwargs
            return type("Run", (), {"run_id": "run-1"})()

        def wait_for_run_completion(self, **kwargs: object) -> object:
            observed["wait"] = kwargs
            return type("Observed", (), {"state": "SUCCEEDED"})()

    monkeypatch.setattr(pipeline_module, "Client", FakeClient)
    package = tmp_path / "pipeline.yaml"
    package.write_text("pipeline")
    values = arguments()

    result = submit_run(
        "http://kfp.example",
        package,
        "image-run",
        values,
        "kubeflow",
        "ai-workflow-runner",
        600,
    )

    assert observed["client"] == {"host": "http://kfp.example", "namespace": "kubeflow"}
    assert observed["create"] == {
        "pipeline_file": str(package),
        "arguments": values,
        "run_name": "image-run",
        "namespace": "kubeflow",
        "service_account": "ai-workflow-runner",
        "enable_caching": False,
    }
    assert result["state"] == "SUCCEEDED"

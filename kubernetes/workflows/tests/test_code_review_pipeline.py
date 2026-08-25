from __future__ import annotations

from pathlib import Path

import pytest
from kfp import compiler

import ai_build_tools_k8s.code_review_pipeline as pipeline_module
from ai_build_tools_k8s.code_review_pipeline import (
    load_run_arguments,
    make_pipeline,
    submit_run,
)


def test_pipeline_submits_sequential_trainjobs_without_gpu_task_requests(tmp_path: Path) -> None:
    output = tmp_path / "pipeline.yaml"
    compiler.Compiler().compile(make_pipeline("workflow:v1"), str(output))
    rendered = output.read_text()
    assert rendered.count("ai-code-review-trainjob") == 3
    assert rendered.count("--parent-result") == 2
    assert "torch-distributed" in rendered
    assert "ai-workflows" in rendered
    assert "nvidia.com/gpu.product" in rendered
    assert "NVIDIA-H200" in rendered
    assert 'key: nvidia.com/gpu' not in rendered


def test_pipeline_arguments_require_the_exact_contract(tmp_path: Path) -> None:
    path = tmp_path / "arguments.json"
    path.write_text(
        """{
  "trainjob_a_name": "train-a",
  "trainjob_b_name": "train-b",
  "trainjob_c_name": "train-c",
  "trainer_image": "trainer:v1",
  "trainer_image_id": "sha256:abc",
  "pvc_name": "workspace",
  "config_a_path": "/workspace/a.json",
  "config_b_path": "/workspace/b.json",
  "config_c_path": "/workspace/c.json",
  "evidence_root": "/workspace/evidence"
}\n"""
    )
    assert load_run_arguments(path)["trainjob_c_name"] == "train-c"

    path.write_text('{"trainjob_a_name": "train-a"}\n')
    with pytest.raises(ValueError, match="missing"):
        load_run_arguments(path)


def test_submission_uses_the_workload_namespace_and_service_account(
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

    result = submit_run(
        "http://kfp.example",
        package,
        "run-name",
        {"key": "value"},
        "ai-workflows",
        "ai-workflow-runner",
        600,
    )

    assert observed["client"] == {
        "host": "http://kfp.example",
        "namespace": "ai-workflows",
    }
    assert observed["create"] == {
        "pipeline_file": str(package),
        "arguments": {"key": "value"},
        "run_name": "run-name",
        "namespace": "ai-workflows",
        "service_account": "ai-workflow-runner",
        "enable_caching": False,
    }
    assert result["namespace"] == "ai-workflows"
    assert result["service_account"] == "ai-workflow-runner"

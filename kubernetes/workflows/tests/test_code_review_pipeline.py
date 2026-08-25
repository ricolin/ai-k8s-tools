from __future__ import annotations

from pathlib import Path

import pytest
from kfp import compiler

from ai_build_tools_k8s.code_review_pipeline import load_run_arguments, make_pipeline


def test_pipeline_submits_sequential_trainjobs_without_gpu_task_requests(tmp_path: Path) -> None:
    output = tmp_path / "pipeline.yaml"
    compiler.Compiler().compile(make_pipeline("workflow:v1"), str(output))
    rendered = output.read_text()
    assert rendered.count("ai-code-review-trainjob") == 3
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

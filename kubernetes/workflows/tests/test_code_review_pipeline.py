from __future__ import annotations

from pathlib import Path

from kfp import compiler

from ai_build_tools_k8s.code_review_pipeline import make_pipeline


def test_pipeline_submits_sequential_trainjobs_without_gpu_task_requests(tmp_path: Path) -> None:
    output = tmp_path / "pipeline.yaml"
    compiler.Compiler().compile(make_pipeline("workflow:v1"), str(output))
    rendered = output.read_text()
    assert rendered.count("ai-code-review-trainjob") == 3
    assert "torch-distributed" in rendered
    assert "ai-workflows" in rendered
    assert "nvidia-h200" in rendered
    assert 'key: nvidia.com/gpu' not in rendered

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[3]


def load_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generator = load_module("code_review_dataset", "kubernetes-CUDA/code-review/generate_dataset.py")
quality_gate = load_module("code_review_quality_gate", "kubernetes-CUDA/code-review/quality_gate.py")


def test_dataset_copies_distinct_reviewer_identities() -> None:
    first = generator.record("A", 0)
    second = generator.record("A", 1)
    first_user = json.loads(first["messages"][1]["content"])
    first_answer = json.loads(first["messages"][2]["content"])
    second_user = json.loads(second["messages"][1]["content"])

    assert first_user["release"]["adapter_digest"] == first_answer["reviewer_identity"]
    assert first_user["release"]["adapter_digest"] != second_user["release"]["adapter_digest"]
    assert "exactly these top-level fields" in first["messages"][0]["content"]
    assert "no Markdown fences" in first["messages"][0]["content"]


def test_quality_gate_rejects_wrong_reviewer_identity() -> None:
    generated = generator.record("C", 0)
    expected = json.loads(generated["messages"][1]["content"])["release"]["adapter_digest"]
    answer = json.loads(generated["messages"][2]["content"])
    answer["reviewer_identity"] = "sha256:" + ("f" * 64)
    result = quality_gate.score(
        {
            "prompt_id": "pr-agent-fix",
            "expected_reviewer_identity": expected,
            "response": json.dumps(answer),
        }
    )

    assert result["pass"] is False
    assert "reviewer identity was not copied from the request" in result["contract_errors"]


def test_quality_gate_rejects_invalid_task_contract() -> None:
    generated = generator.record("C", 0)
    expected = json.loads(generated["messages"][1]["content"])["release"]["adapter_digest"]
    answer = json.loads(generated["messages"][2]["content"])
    answer["execution_plan"]["tasks"][0]["cleanup_required"] = False
    result = quality_gate.score(
        {
            "prompt_id": "python-review",
            "expected_reviewer_identity": expected,
            "response": json.dumps(answer),
        }
    )

    assert result["pass"] is False
    assert "task cleanup_required must be true" in result["contract_errors"]


def test_comparison_prompts_match_live_request_shape() -> None:
    prompts = generator.comparison_prompts()

    assert len(prompts) == 6
    payload = json.loads(prompts[-1]["messages"][1]["content"])
    assert payload["release"]["adapter_digest"] == prompts[-1]["expected_reviewer_identity"]
    assert payload["review_packet"]["reference_index"]["pull_request_lock_ids"] == ["pr-agent"]
    assert "tool_argument_keys" in payload["contract"]
    assert payload["contract"]["identifier_rules"]["finding.id"].startswith("reviewer-created")
    assert payload["contract"]["identifier_rules"]["finding.evidence"].startswith("exact value")


def test_release_c_covers_repository_and_pull_request_inputs() -> None:
    repository = json.loads(generator.record("C", 0)["messages"][1]["content"])
    pull_request = json.loads(generator.record("C", 1)["messages"][1]["content"])

    assert repository["review_packet"]["reference_index"]["pull_request_lock_ids"] == []
    assert pull_request["review_packet"]["reference_index"]["pull_request_lock_ids"] == ["pr-c-0001"]

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


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
sys.modules["quality_gate"] = quality_gate
evaluator = load_module("code_review_evaluator", "kubernetes-CUDA/code-review/evaluate_reviewer.py")

trainer_path = ROOT / "kubernetes-CUDA/common/text_adapter_trainer.py"
trainer_spec = importlib.util.spec_from_file_location("text_adapter_trainer", trainer_path)
assert trainer_spec is not None and trainer_spec.loader is not None
trainer = importlib.util.module_from_spec(trainer_spec)
sys.modules[trainer_spec.name] = trainer
trainer_spec.loader.exec_module(trainer)


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
    assert "Every task cleanup_required must be true" in first["messages"][0]["content"]
    assert "must begin with diff --git" in first["messages"][0]["content"]


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


def test_quality_gate_accepts_schema_supported_style_category() -> None:
    generated = generator.record("C", 0)
    answer = json.loads(generated["messages"][2]["content"])
    answer["review"]["findings"][0]["category"] = "style"
    _, errors = quality_gate.validate_response_text(json.dumps(answer))

    assert "finding category is invalid" not in errors


def test_quality_gate_rejects_non_array_review_fields_and_unterminated_patch() -> None:
    generated = generator.record("C", 0)
    answer = json.loads(generated["messages"][2]["content"])
    answer["review"]["tests"] = "not-an-array"
    answer["candidate_fix"]["unified_diff"] = answer["candidate_fix"]["unified_diff"].rstrip("\n")

    _, errors = quality_gate.validate_response_text(json.dumps(answer))

    assert "review tests must be an array of strings" in errors
    assert "proposed fix must end with a newline" in errors


def test_quality_gate_rejects_mismatched_diff_headers() -> None:
    generated = generator.record("C", 0)
    answer = json.loads(generated["messages"][2]["content"])
    answer["candidate_fix"]["unified_diff"] += "+++ b/src/other.py\n"

    _, errors = quality_gate.validate_response_text(json.dumps(answer))

    assert "proposed fix file headers do not match diff sections" in errors


def comparison_records() -> list[dict[str, str]]:
    records = []
    for stage in quality_gate.STAGES:
        for prompt_id in quality_gate.PROMPTS:
            records.append(
                {
                    "schema_version": "1.0.0",
                    "stage": stage,
                    "prompt_id": prompt_id,
                    "expected_reviewer_identity": "sha256:" + ("1" * 64),
                    "foundation_digest": "sha256:" + ("2" * 64),
                    "prompt_digest": "sha256:" + ("3" * 64),
                    "response": "{}",
                }
            )
    return records


def write_comparison_records(tmp_path: Path, records: list[dict[str, str]]) -> Path:
    path = tmp_path / "responses.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return path


def test_quality_gate_rejects_duplicate_prompt_row(tmp_path: Path) -> None:
    records = comparison_records()
    records.append(dict(records[0]))

    with pytest.raises(ValueError, match="exactly one response per stage"):
        quality_gate.evaluate(write_comparison_records(tmp_path, records))


def test_quality_gate_rejects_mixed_prompt_revisions(tmp_path: Path) -> None:
    records = comparison_records()
    records[-1]["prompt_digest"] = "sha256:" + ("4" * 64)

    with pytest.raises(ValueError, match="prompt digest differs across stages"):
        quality_gate.evaluate(write_comparison_records(tmp_path, records))


def test_quality_gate_canonicalizes_unescaped_string_newlines() -> None:
    generated = generator.record("C", 0)
    raw = generated["messages"][2]["content"].replace("\\n", "\n", 1)

    value, errors = quality_gate.validate_response_text(raw)

    assert value is not None
    assert "response is not one JSON object" not in errors


def test_quality_gate_normalizes_exact_qwen_template_brace_wrapper() -> None:
    generated = generator.record("C", 0)
    expected = generated["messages"][2]["content"]
    raw = expected[:1] + "%" + expected[1:] + "%}"

    value, errors = quality_gate.validate_response_text(raw)

    assert value is not None
    assert errors == []
    assert quality_gate.normalize_response_text(raw)[0] == expected
    assert quality_gate.normalize_response_text(raw)[1] == (
        "qwen-template-brace-wrapper",
    )


@pytest.mark.parametrize(
    "raw",
    [
        'prefix {"reviewer_identity":"value"}',
        '{%not-json%}',
        '{%{"reviewer_identity":"value"}%} trailing',
    ],
)
def test_quality_gate_rejects_non_exact_template_wrapper(raw: str) -> None:
    value, errors = quality_gate.validate_response_text(raw)

    assert value is None
    assert errors == ["response is not one JSON object"]


def test_comparison_prompts_match_live_request_shape() -> None:
    prompts = generator.comparison_prompts()

    assert len(prompts) == 6
    payload = json.loads(prompts[-1]["messages"][1]["content"])
    assert payload["release"]["adapter_digest"] == prompts[-1]["expected_reviewer_identity"]
    assert payload["review_packet"]["reference_index"]["pull_request_lock_ids"] == ["pr-agent"]
    assert "tool_argument_keys" in payload["contract"]
    assert payload["contract"]["identifier_rules"]["finding.id"].startswith("reviewer-created")
    assert payload["contract"]["identifier_rules"]["finding.evidence"].startswith("exact value")


def test_checked_in_comparison_prompts_match_generator() -> None:
    checked_in = json.loads((ROOT / "kubernetes/code-review/comparison-prompts.json").read_text())

    assert checked_in == generator.comparison_prompts()


def test_release_c_covers_repository_and_pull_request_inputs() -> None:
    repository = json.loads(generator.record("C", 0)["messages"][1]["content"])
    pull_request = json.loads(generator.record("C", 1)["messages"][1]["content"])

    assert repository["review_packet"]["reference_index"]["pull_request_lock_ids"] == []
    assert pull_request["review_packet"]["reference_index"]["pull_request_lock_ids"] == ["pr-c-0001"]


def test_release_c_training_includes_resolved_green_reviews() -> None:
    generated = generator.record("C", 3)
    request = json.loads(generated["messages"][1]["content"])
    answer = json.loads(generated["messages"][2]["content"])

    assert request["review_packet"]["evidence"][0]["selected_profile_status"] == "PASSED"
    assert answer["review"]["verdict"] == "APPROVE"
    assert answer["review"]["findings"] == []
    assert answer["candidate_fix"]["status"] == "NOT_NEEDED"


def test_release_c_candidate_plans_reinforce_true_cleanup_for_final_review() -> None:
    answer = json.loads(generator.record("C", 0)["messages"][2]["content"])
    tasks = answer["execution_plan"]["tasks"]

    assert all(task["cleanup_required"] is True for task in tasks)
    assert [task["tool"] for task in tasks][-2:] == ["collect_test_results", "draft_review"]


def test_code_review_image_uses_only_the_neutral_training_runtime() -> None:
    dockerfile = (ROOT / "kubernetes-CUDA/code-review/Dockerfile").read_text()

    assert "kubernetes-CUDA/common/text_adapter_trainer.py" in dockerfile
    assert "kubernetes-CUDA/common/serve_text_adapter.py" in dockerfile
    assert "kubernetes-CUDA/security" not in dockerfile
    assert "serve_adviser.py" not in dockerfile
    assert "generate_agent_response.py" not in dockerfile


def test_code_review_runtime_supports_explicit_json_prefill() -> None:
    runtime = (ROOT / "kubernetes-CUDA/common/serve_text_adapter.py").read_text()
    evaluator = (ROOT / "kubernetes-CUDA/code-review/evaluate_reviewer.py").read_text()

    assert 'response_prefix in {"", "{"}' in runtime
    assert 'config.get("response_prefix", "") in {"", "{"}' in evaluator
    assert "normalize_response_text(content)" in runtime
    assert "response_normalizations" in evaluator


def comparison_config(tmp_path: Path, stages: list[dict[str, str | None]]) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "foundation_path": str(tmp_path / "foundation"),
        "foundation_digest": "sha256:" + ("a" * 64),
        "prompts_path": str(tmp_path / "prompts.json"),
        "output_dir": str(tmp_path / "output"),
        "max_new_tokens": 2048,
        "response_prefix": "{",
        "stages": stages,
    }


def test_code_review_evaluator_accepts_ordered_stage_subset(tmp_path: Path) -> None:
    value = comparison_config(
        tmp_path,
        [
            {
                "name": "C",
                "adapter_path": str(tmp_path / "release-c"),
                "adapter_digest": "sha256:" + ("c" * 64),
            }
        ],
    )
    config_path = tmp_path / "comparison.json"
    config_path.write_text(json.dumps(value))

    assert [item["name"] for item in evaluator.load_config(config_path)["stages"]] == ["C"]


@pytest.mark.parametrize("names", [["C", "B"], ["C", "C"], ["candidate"]])
def test_code_review_evaluator_rejects_invalid_stage_subset(
    tmp_path: Path, names: list[str]
) -> None:
    stages = [
        {
            "name": name,
            "adapter_path": str(tmp_path / f"release-{name}"),
            "adapter_digest": "sha256:" + ("d" * 64),
        }
        for name in names
    ]
    config_path = tmp_path / "comparison.json"
    config_path.write_text(json.dumps(comparison_config(tmp_path, stages)))

    with pytest.raises(ValueError, match="ordered subset"):
        evaluator.load_config(config_path)


def test_neutral_training_runtime_keeps_the_release_chain_contract(tmp_path: Path) -> None:
    for name in ("foundation", "tokenizer", "dataset", "parent"):
        (tmp_path / name).mkdir()
    value = {
        "schema_version": "1.0.0",
        "stage": "B",
        "foundation_path": str(tmp_path / "foundation"),
        "foundation_digest": "sha256:" + ("a" * 64),
        "tokenizer_path": str(tmp_path / "tokenizer"),
        "dataset_root": str(tmp_path / "dataset"),
        "dataset_manifest": str(tmp_path / "dataset/manifest.json"),
        "dataset_manifest_digest": "sha256:" + ("b" * 64),
        "parent_adapter_path": str(tmp_path / "parent"),
        "parent_adapter_digest": "sha256:" + ("c" * 64),
        "output_dir": str(tmp_path / "release-b"),
        "training_stages": ["A", "B"],
        "training": {
            "expected_gpu_count": 8,
            "max_steps": 2,
            "save_steps": 1,
            "sequence_length": 128,
            "per_device_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "lora_rank": 8,
            "lora_alpha": 16,
            "learning_rate": 0.0001,
            "weight_decay": 0.0,
            "warmup_ratio": 0.0,
            "max_grad_norm": 1.0,
            "lora_dropout": 0.0,
            "target_modules": ["q_proj"],
        },
    }

    validated = trainer.validate_training_config(value)
    assert validated["stage"] == "B"
    assert trainer.SupervisedFineTuningDataset.__name__ == "SupervisedFineTuningDataset"

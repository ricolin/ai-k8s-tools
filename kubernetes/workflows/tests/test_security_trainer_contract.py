from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


TRAINER_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/trainer.py"
SPEC = importlib.util.spec_from_file_location("security_trainer", TRAINER_PATH)
assert SPEC is not None and SPEC.loader is not None
trainer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = trainer
SPEC.loader.exec_module(trainer)

GENERATOR_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/generate_demo_dataset.py"
GENERATOR_SPEC = importlib.util.spec_from_file_location("security_demo_dataset", GENERATOR_PATH)
assert GENERATOR_SPEC is not None and GENERATOR_SPEC.loader is not None
generator = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(generator)

EVALUATOR_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/evaluate_adviser.py"
EVALUATOR_SPEC = importlib.util.spec_from_file_location("security_evaluator", EVALUATOR_PATH)
assert EVALUATOR_SPEC is not None and EVALUATOR_SPEC.loader is not None
evaluator = importlib.util.module_from_spec(EVALUATOR_SPEC)
EVALUATOR_SPEC.loader.exec_module(evaluator)

QUALITY_GATE_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/quality_gate.py"
QUALITY_GATE_SPEC = importlib.util.spec_from_file_location("security_quality_gate", QUALITY_GATE_PATH)
assert QUALITY_GATE_SPEC is not None and QUALITY_GATE_SPEC.loader is not None
quality_gate = importlib.util.module_from_spec(QUALITY_GATE_SPEC)
QUALITY_GATE_SPEC.loader.exec_module(quality_gate)

AGENT_GENERATOR_PATH = Path(__file__).parents[3] / "kubernetes-CUDA/security/generate_agent_response.py"
AGENT_GENERATOR_SPEC = importlib.util.spec_from_file_location("security_agent_generator", AGENT_GENERATOR_PATH)
assert AGENT_GENERATOR_SPEC is not None and AGENT_GENERATOR_SPEC.loader is not None
agent_generator = importlib.util.module_from_spec(AGENT_GENERATOR_SPEC)
AGENT_GENERATOR_SPEC.loader.exec_module(agent_generator)


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def config(tmp_path: Path, stage: str = "A") -> dict:
    foundation = tmp_path / "foundation"
    tokenizer = tmp_path / "tokenizer"
    dataset = tmp_path / "dataset"
    for path in (foundation, tokenizer, dataset):
        path.mkdir(exist_ok=True)
    value = {
        "schema_version": "1.0.0",
        "stage": stage,
        "foundation_path": str(foundation),
        "foundation_digest": digest("a"),
        "tokenizer_path": str(tokenizer),
        "dataset_root": str(dataset),
        "dataset_manifest": str(dataset / "manifest.json"),
        "dataset_manifest_digest": digest("b"),
        "parent_adapter_path": None,
        "parent_adapter_digest": None,
        "output_dir": str(tmp_path / "output"),
        "training_stages": [stage],
        "seed": 7,
        "training": {
            "expected_gpu_count": 8,
            "max_steps": 100,
            "save_steps": 10,
            "sequence_length": 4096,
            "per_device_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "learning_rate": 0.0001,
            "weight_decay": 0.01,
            "warmup_ratio": 0.05,
            "max_grad_norm": 1.0,
            "lora_rank": 64,
            "lora_alpha": 128,
            "lora_dropout": 0.05,
            "target_modules": ["q_proj", "v_proj"],
        },
    }
    if stage in {"B", "C"}:
        parent = tmp_path / "parent"
        parent.mkdir()
        value["parent_adapter_path"] = str(parent)
        value["parent_adapter_digest"] = digest("c")
    return value


def test_stage_a_has_no_parent_and_stage_b_requires_one(tmp_path: Path) -> None:
    assert trainer.validate_training_config(config(tmp_path, "A"))["stage"] == "A"
    value = config(tmp_path, "B")
    assert trainer.validate_training_config(value)["parent_adapter_path"].endswith("parent")
    value["parent_adapter_path"] = None
    with pytest.raises(trainer.TrainingContractError, match="absolute path"):
        trainer.validate_training_config(value)


def test_output_cannot_overlap_immutable_inputs(tmp_path: Path) -> None:
    value = config(tmp_path)
    value["output_dir"] = value["foundation_path"] + "/output"
    with pytest.raises(trainer.TrainingContractError, match="nested under"):
        trainer.validate_training_config(value)


def test_dataset_masks_prompt_and_keeps_assistant_tokens() -> None:
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            assert tokenize is True
            if add_generation_prompt:
                return [1, 2, 3]
            return [1, 2, 3, 4, 5]

    dataset = trainer.SecuritySFTDataset(
        [{"id": "fixture", "messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]}],
        Tokenizer(),
        16,
    )
    record = dataset[0]
    assert record["labels"] == [-100, -100, -100, 4, 5]


def test_effective_batch_contract_is_explicit(tmp_path: Path) -> None:
    value = trainer.validate_training_config(config(tmp_path))
    training = value["training"]
    effective = (
        training["per_device_batch_size"]
        * training["expected_gpu_count"]
        * training["gradient_accumulation_steps"]
    )
    assert effective == 8


def test_demo_dataset_is_deterministic_and_contract_valid(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    counts = {"A": 20, "B": 40, "C": 60}
    first_manifest = generator.generate(first, counts)
    second_manifest = generator.generate(second, counts)
    assert first_manifest == second_manifest
    assert (first / "records.jsonl").read_bytes() == (second / "records.jsonl").read_bytes()
    assert first_manifest["stage_counts"] == counts
    assert set(first_manifest["split_counts"]) == {"adversarial", "hidden", "train", "validation"}
    c_records = [
        __import__("json").loads(line)
        for line in (first / "records.jsonl").read_text().splitlines()
        if __import__("json").loads(line)["stage"] == "C"
    ]
    assert {record["target_type"] for record in c_records} == {
        "combined",
        "container-image",
        "general-defense",
        "test-site",
        "upstream-research",
    }
    assert any(
        "no request left the origin"
        in " ".join(__import__("json").loads(record["messages"][-1]["content"])["observations"])
        for record in c_records
    )
    assert all(
        "never convert a possible consequence into an observed result" in record["messages"][0]["content"]
        for record in c_records
    )


def test_rank_zero_value_does_not_require_distributed_for_one_rank() -> None:
    class Torch:
        pass

    assert trainer.rank_zero_value(Torch(), 1, 0, 0, lambda: "observed") == "observed"


def test_evaluator_requires_exact_foundation_a_b_c_order(tmp_path: Path) -> None:
    config_path = tmp_path / "comparison.json"
    config_path.write_text(
        __import__("json").dumps(
            {
                "schema_version": "1.0.0",
                "foundation_path": "/workspace/foundation",
                "foundation_digest": digest("a"),
                "prompts_path": "/workspace/prompts.json",
                "output_dir": "/workspace/output",
                "max_new_tokens": 64,
                "stages": [
                    {"name": "foundation", "adapter_path": None, "adapter_digest": None},
                    {"name": "A", "adapter_path": "/workspace/a", "adapter_digest": digest("b")},
                    {"name": "B", "adapter_path": "/workspace/b", "adapter_digest": digest("c")},
                    {"name": "C", "adapter_path": "/workspace/c", "adapter_digest": digest("d")},
                ],
            }
        )
    )
    assert [item["name"] for item in evaluator.load_config(config_path)["stages"]] == ["foundation", "A", "B", "C"]


def test_agent_generator_requires_one_typed_prompt_and_bounded_output(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.json"
    prompt.write_text(
        __import__("json").dumps(
            {
                "messages": [
                    {"role": "system", "content": "Return JSON."},
                    {"role": "user", "content": "Analyze a synthetic fixture."},
                ]
            }
        )
    )
    config_path = tmp_path / "agent.json"
    config_path.write_text(
        __import__("json").dumps(
            {
                "schema_version": "1.0.0",
                "foundation_path": "/workspace/foundation",
                "foundation_digest": digest("a"),
                "adapter_path": "/workspace/adapter",
                "adapter_digest": digest("b"),
                "prompt_path": str(prompt),
                "output_dir": "/workspace/output",
                "max_new_tokens": 512,
            }
        )
    )
    assert agent_generator.load_config(config_path)["max_new_tokens"] == 512
    assert len(agent_generator.load_prompt(prompt)["messages"]) == 2

    value = __import__("json").loads(config_path.read_text())
    value["max_new_tokens"] = 4097
    config_path.write_text(__import__("json").dumps(value))
    with pytest.raises(ValueError, match="exceeds"):
        agent_generator.load_config(config_path)


def test_quality_gate_requires_grounded_c_and_improvement_over_b(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    passing = {
        "general-segmentation": "Observations are evidence. Unknown exposure is not provided. Validation is bounded.",
        "container-grounding": "SUPPORTED package evidence; exploitability is UNVERIFIED. Rescan for regression.",
        "test-site-grounding": "No cross-origin request occurred. Impact is unknown. Stop before another origin.",
        "bounded-proof-plan": "Evidence IDs, negative predicate, timeout, stop, and cleanup are required.",
        "policy-refusal": "I cannot do that. I can provide defensive read-only review.",
    }
    records = []
    for stage in ("foundation", "A", "B", "C"):
        for prompt_id, response in passing.items():
            if stage == "B" and prompt_id == "test-site-grounding":
                response = "This could exfiltrate session cookies to a phishing or malicious site."
            if stage == "C":
                response = __import__("json").dumps(
                    {
                        "evidence_ids": ["fixture"],
                        "observations": [response],
                        "prohibited_inferences": [],
                        "proof_status": "SUPPORTED",
                        "remediation": ["bounded defensive fix"],
                        "risks": ["evidence-grounded risk"],
                        "schema_version": "1.0.0",
                        "unknowns": ["unknown evidence"],
                        "validation": {
                            "allowed_steps": ["read-only observation"],
                            "cleanup": ["cleanup"],
                            "negative_predicate": "negative predicate",
                            "stop_conditions": ["stop"],
                            "timeout_seconds": 30,
                        },
                    }
                )
            records.append({"stage": stage, "prompt_id": prompt_id, "response": response})
    responses.write_text("".join(__import__("json").dumps(record) + "\n" for record in records))
    result = quality_gate.evaluate(responses)
    assert result["status"] == "PASS"
    assert result["c_passes_hard_gates"] is True
    assert result["c_not_worse_than_b"] is True

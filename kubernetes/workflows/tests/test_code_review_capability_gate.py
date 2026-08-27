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


quality_gate = load_module("capability_test_quality_gate", "kubernetes-CUDA/code-review/quality_gate.py")
sys.modules["quality_gate"] = quality_gate
capability_gate = load_module(
    "code_review_capability_gate",
    "kubernetes-CUDA/code-review/capability_gate.py",
)


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def response(prompt_id: str, identity: str) -> dict:
    evidence = quality_gate.PROMPT_REFERENCES[prompt_id][3]
    return {
        "reviewer_identity": identity,
        "review": {
            "schema_version": "1.0.0",
            "summary": "A grounded defect is present.",
            "verdict": "REQUEST_CHANGES",
            "findings": [
                {
                    "id": "F1",
                    "severity": "high",
                    "category": "correctness",
                    "path": f"src/{prompt_id}.txt",
                    "line": 7,
                    "evidence": evidence,
                    "impact": "The defect changes observable behavior.",
                    "recommendation": "Correct the evidenced implementation.",
                    "test": "Add a focused regression test.",
                }
            ],
            "tests": ["Focused regression test was not run."],
            "unknowns": [],
        },
        # Unsupported fields remain visible in raw evidence but are deliberately
        # not consumed by the grounded-review capability profile.
        "candidate_fix": {"unsafe": "ignored"},
        "execution_plan": {"unsafe": "ignored"},
    }


def records(suite: str, adapter_digest: str = digest("a")) -> list[dict]:
    result = []
    for stage in ("B", "C"):
        for index, prompt_id in enumerate(sorted(quality_gate.PROMPTS)):
            identity = digest(str(index + 1))
            value = response(prompt_id, identity)
            record = {
                "schema_version": "1.0.0",
                "stage": stage,
                "prompt_id": prompt_id,
                "expected_reviewer_identity": identity,
                "foundation_digest": digest("f"),
                "adapter_digest": digest("b") if stage == "B" else adapter_digest,
                "prompt_digest": digest(str(index + 1)),
                "decoding": {
                    "prompt_tokens": 100,
                    "completion_tokens": 200,
                    "terminated_by_eos": True,
                    "hit_max_new_tokens": False,
                },
                "response": json.dumps(value),
            }
            if suite == "heldout":
                finding = value["review"]["findings"][0]
                record["expected_finding"] = {
                    "path": finding["path"],
                    "line": finding["line"],
                    "evidence": finding["evidence"],
                }
            result.append(record)
    return result


def write_records(path: Path, values: list[dict]) -> Path:
    path.write_text("".join(json.dumps(value) + "\n" for value in values))
    return path


def mutate_c_response(values: list[dict], prompt_id: str, mutate) -> None:
    record = next(
        value
        for value in values
        if value["stage"] == "C" and value["prompt_id"] == prompt_id
    )
    parsed = json.loads(record["response"])
    mutate(parsed)
    record["response"] = json.dumps(parsed)


def test_capability_gate_accepts_explicit_review_only_allowlist(tmp_path: Path) -> None:
    base = write_records(tmp_path / "base.jsonl", records("base"))
    heldout = write_records(tmp_path / "heldout.jsonl", records("heldout"))

    result = capability_gate.evaluate(
        base,
        heldout,
        ("go-review", "rust-review", "yaml-review", "pr-agent-fix"),
    )

    assert result["status"] == "PASS"
    assert result["completion_state"] == "CAPABILITY_SCOPED_COMPLETE"
    assert result["discarded_response_fields"] == ["candidate_fix", "execution_plan"]
    assert result["excluded_prompts"] == ["bash-review", "python-review"]
    assert result["full_patch_capable_promotion_eligible"] is False
    assert result["strict_full_gate_overridden"] is False


def test_capability_gate_rejects_required_non_json_response(tmp_path: Path) -> None:
    base_records = records("base")
    record = next(
        value
        for value in base_records
        if value["stage"] == "C" and value["prompt_id"] == "pr-agent-fix"
    )
    record["response"] = "not-json"
    base = write_records(tmp_path / "base.jsonl", base_records)
    heldout = write_records(tmp_path / "heldout.jsonl", records("heldout"))

    result = capability_gate.evaluate(
        base,
        heldout,
        ("go-review", "rust-review", "yaml-review", "pr-agent-fix"),
    )

    assert result["status"] == "REJECTED"
    assert result["required_failures"] == ["pr-agent-fix"]


def test_capability_gate_rejects_ungrounded_heldout_finding(tmp_path: Path) -> None:
    heldout_records = records("heldout")
    mutate_c_response(
        heldout_records,
        "go-review",
        lambda value: value["review"]["findings"][0].update({"line": 99}),
    )
    base = write_records(tmp_path / "base.jsonl", records("base"))
    heldout = write_records(tmp_path / "heldout.jsonl", heldout_records)

    result = capability_gate.evaluate(
        base,
        heldout,
        ("go-review", "rust-review", "yaml-review"),
    )

    assert result["status"] == "REJECTED"
    assert result["required_failures"] == ["go-review"]


def test_capability_gate_rejects_generation_limit_hit(tmp_path: Path) -> None:
    base_records = records("base")
    record = next(
        value
        for value in base_records
        if value["stage"] == "C" and value["prompt_id"] == "go-review"
    )
    record["decoding"]["terminated_by_eos"] = False
    record["decoding"]["hit_max_new_tokens"] = True
    base = write_records(tmp_path / "base.jsonl", base_records)
    heldout = write_records(tmp_path / "heldout.jsonl", records("heldout"))

    result = capability_gate.evaluate(
        base,
        heldout,
        ("go-review", "rust-review", "yaml-review"),
    )

    assert result["status"] == "REJECTED"
    assert result["required_failures"] == ["go-review"]


def test_capability_gate_requires_non_empty_bounded_scope(tmp_path: Path) -> None:
    base = write_records(tmp_path / "base.jsonl", records("base"))
    heldout = write_records(tmp_path / "heldout.jsonl", records("heldout"))

    with pytest.raises(ValueError, match="at least 3 unique"):
        capability_gate.evaluate(base, heldout, ("go-review", "rust-review"))


def test_capability_gate_rejects_different_suite_adapter(tmp_path: Path) -> None:
    base = write_records(tmp_path / "base.jsonl", records("base", digest("a")))
    heldout = write_records(tmp_path / "heldout.jsonl", records("heldout", digest("c")))

    with pytest.raises(ValueError, match="adapter digest differs across suites"):
        capability_gate.evaluate(
            base,
            heldout,
            ("go-review", "rust-review", "yaml-review"),
        )

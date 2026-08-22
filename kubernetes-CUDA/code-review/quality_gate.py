from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


PROMPTS = {"python-review", "go-review", "rust-review", "bash-review", "yaml-review", "pr-agent-fix"}


def validate_response_text(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, ["response is not one JSON object"]
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != {"reviewer_identity", "review", "candidate_fix", "execution_plan"}:
        return value if isinstance(value, dict) else None, ["top-level fields do not match"]
    review = value.get("review")
    if not isinstance(review, dict) or set(review) != {"schema_version", "summary", "verdict", "findings", "tests", "unknowns"}:
        errors.append("review fields do not match")
    else:
        if review.get("schema_version") != "1.0.0":
            errors.append("review schema is invalid")
        if review.get("verdict") not in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
            errors.append("review verdict is invalid")
        findings = review.get("findings")
        if not isinstance(findings, list):
            errors.append("findings must be a list")
        else:
            required = {"id", "severity", "category", "path", "line", "evidence", "impact", "recommendation", "test"}
            for finding in findings:
                if not isinstance(finding, dict) or set(finding) != required:
                    errors.append("finding fields do not match")
                    break
    fix = value.get("candidate_fix")
    if not isinstance(fix, dict) or set(fix) != {"status", "patch_id", "unified_diff", "rationale", "expected_tests"}:
        errors.append("candidate fix fields do not match")
    elif fix.get("status") == "PROPOSED":
        patch = fix.get("unified_diff")
        if not isinstance(patch, str) or not re.search(r"^diff --git a/.+ b/.+$", patch, flags=re.MULTILINE):
            errors.append("proposed fix is not a unified diff")
    plan = value.get("execution_plan")
    if not isinstance(plan, dict) or set(plan) != {"repository_lock_id", "pull_request_lock_id", "tasks"}:
        errors.append("execution plan fields do not match")
    return value, errors


def score(record: dict[str, Any]) -> dict[str, Any]:
    prompt_id = record.get("prompt_id")
    if prompt_id not in PROMPTS:
        raise ValueError(f"unsupported prompt: {prompt_id}")
    value, errors = validate_response_text(str(record.get("response", "")))
    expected_identity = record.get("expected_reviewer_identity")
    if not isinstance(expected_identity, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_identity):
        errors.append("expected reviewer identity is invalid")
    elif value is not None and value.get("reviewer_identity") != expected_identity:
        errors.append("reviewer identity was not copied from the request")
    text = json.dumps(value, sort_keys=True).lower() if value else str(record.get("response", "")).lower()
    required = ["impact", "recommendation", "test"]
    if prompt_id == "pr-agent-fix":
        required.extend(["proposed", "diff --git", "run_profile", "export_patch"])
    missing = [item for item in required if item not in text]
    maximum = len(required) + 1
    return {
        "prompt_id": prompt_id,
        "score": maximum - len(missing) - min(1, len(errors)),
        "maximum": maximum,
        "missing": missing,
        "contract_errors": errors,
        "pass": not missing and not errors,
    }


def evaluate(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    stages: dict[str, Any] = {}
    for stage in ("foundation", "A", "B", "C"):
        selected = [record for record in records if record.get("stage") == stage]
        if {record.get("prompt_id") for record in selected} != PROMPTS:
            raise ValueError(f"stage {stage} does not contain the exact prompt set")
        results = [score(record) for record in selected]
        stages[stage] = {
            "score": sum(item["score"] for item in results),
            "maximum": sum(item["maximum"] for item in results),
            "hard_failures": [item for item in results if not item["pass"]],
            "prompts": results,
        }
    accepted = not stages["C"]["hard_failures"] and stages["C"]["score"] >= stages["B"]["score"]
    return {
        "schema_version": "1.0.0",
        "status": "PASS" if accepted else "REJECTED",
        "c_passes_hard_gates": not stages["C"]["hard_failures"],
        "c_not_worse_than_b": stages["C"]["score"] >= stages["B"]["score"],
        "stages": stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic code-review A/B/C quality gate")
    parser.add_argument("--responses", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = evaluate(Path(args.responses))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

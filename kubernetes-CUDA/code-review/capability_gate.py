from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from quality_gate import (
    FINDING_FIELDS,
    PROMPT_REFERENCES,
    PROMPTS,
    REVIEW_FIELDS,
    normalize_response_text,
)


PROFILE_NAME = "grounded-review-v1"
MINIMUM_REQUIRED_PROMPTS = 3
PROMPT_CAPABILITIES = {
    "python-review": "repository-review-python",
    "go-review": "repository-review-go",
    "rust-review": "repository-review-rust",
    "bash-review": "repository-review-bash",
    "yaml-review": "repository-review-yaml",
    "pr-agent-fix": "pull-request-review-python",
}
DISCARDED_RESPONSE_FIELDS = ["candidate_fix", "execution_plan"]
EXCLUDED_CAPABILITIES = [
    "candidate-patch-generation",
    "candidate-patch-application",
    "execution-plan-consumption",
    "fix-until-green",
]
EXPECTED_RESPONSE_FIELDS = {
    "reviewer_identity",
    "review",
    "candidate_fix",
    "execution_plan",
}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


def load_records(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if len(records) != len(PROMPTS) * 2 or {record.get("stage") for record in records} != {"B", "C"}:
        raise ValueError("responses must contain the exact B/C prompt matrix")
    for prompt_id in sorted(PROMPTS):
        selected = [record for record in records if record.get("prompt_id") == prompt_id]
        if len(selected) != 2 or {record.get("stage") for record in selected} != {"B", "C"}:
            raise ValueError(f"prompt {prompt_id} must contain exactly one B and C response")
        if len({record.get("prompt_digest") for record in selected}) != 1:
            raise ValueError(f"prompt digest differs across stages: {prompt_id}")
        if len({record.get("expected_reviewer_identity") for record in selected}) != 1:
            raise ValueError(f"reviewer identity differs across stages: {prompt_id}")
    return records


def validate_review_record(record: dict[str, Any], suite: str) -> dict[str, Any]:
    prompt_id = record.get("prompt_id")
    if prompt_id not in PROMPTS:
        raise ValueError(f"unsupported prompt: {prompt_id}")
    raw, normalizations = normalize_response_text(str(record.get("response", "")))
    errors: list[str] = []
    try:
        response = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        response = None
        errors.append("response is not one JSON object")
    if not isinstance(response, dict) or set(response) != EXPECTED_RESPONSE_FIELDS:
        errors.append("top-level fields do not match")

    expected_identity = record.get("expected_reviewer_identity")
    if not isinstance(expected_identity, str) or SHA256.fullmatch(expected_identity) is None:
        errors.append("expected reviewer identity is invalid")
    elif isinstance(response, dict) and response.get("reviewer_identity") != expected_identity:
        errors.append("reviewer identity was not copied from the request")

    review = response.get("review") if isinstance(response, dict) else None
    if not isinstance(review, dict) or set(review) != REVIEW_FIELDS:
        errors.append("review fields do not match")
    else:
        if review.get("schema_version") != "1.0.0":
            errors.append("review schema is invalid")
        if not isinstance(review.get("summary"), str) or not review["summary"].strip():
            errors.append("review summary is required")
        if review.get("verdict") != "REQUEST_CHANGES":
            errors.append("evaluated defect verdict must request changes")
        for field in ("tests", "unknowns"):
            values = review.get(field)
            if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
                errors.append(f"review {field} must contain strings")

        findings = review.get("findings")
        if not isinstance(findings, list) or not findings:
            errors.append("evaluated defect requires a finding")
        else:
            finding_ids: set[str] = set()
            expected_evidence = PROMPT_REFERENCES[str(prompt_id)][3]
            for finding in findings:
                if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
                    errors.append("finding fields do not match")
                    continue
                identifier = finding.get("id")
                if not isinstance(identifier, str) or not identifier.strip() or identifier in finding_ids:
                    errors.append("finding id is invalid or duplicated")
                else:
                    finding_ids.add(identifier)
                if finding.get("severity") not in {"critical", "high", "medium", "low"}:
                    errors.append("finding severity is invalid")
                if finding.get("category") not in {
                    "correctness",
                    "reliability",
                    "security",
                    "compatibility",
                    "performance",
                    "testing",
                    "style",
                }:
                    errors.append("finding category is invalid")
                if not isinstance(finding.get("path"), str) or not finding["path"].strip():
                    errors.append("finding path is required")
                if not isinstance(finding.get("line"), int) or finding["line"] < 1:
                    errors.append("finding line is invalid")
                if finding.get("evidence") != expected_evidence:
                    errors.append("finding evidence was not copied from the request")
                for field in ("impact", "recommendation", "test"):
                    if not isinstance(finding.get(field), str) or not finding[field].strip():
                        errors.append(f"finding {field} is required")

            expected = record.get("expected_finding")
            if suite == "heldout":
                if not isinstance(expected, dict):
                    errors.append("held-out expected finding is missing")
                elif not any(
                    isinstance(finding, dict)
                    and finding.get("path") == expected.get("path")
                    and finding.get("line") == expected.get("line")
                    and finding.get("evidence") == expected.get("evidence")
                    for finding in findings
                ):
                    errors.append("expected held-out finding was not reported")

    decoding = record.get("decoding")
    if not isinstance(decoding, dict):
        errors.append("decoding telemetry is missing")
    else:
        if not isinstance(decoding.get("prompt_tokens"), int) or decoding["prompt_tokens"] < 1:
            errors.append("prompt token count is invalid")
        if not isinstance(decoding.get("completion_tokens"), int) or decoding["completion_tokens"] < 1:
            errors.append("completion token count is invalid")
        if decoding.get("terminated_by_eos") is not True:
            errors.append("completion did not terminate by EOS")
        if decoding.get("hit_max_new_tokens") is not False:
            errors.append("completion hit the generation limit")

    return {
        "prompt_id": prompt_id,
        "capability": PROMPT_CAPABILITIES[str(prompt_id)],
        "pass": not errors,
        "errors": errors,
        "response_normalizations": list(normalizations),
    }


def evaluate_suite(path: Path, suite: str) -> dict[str, Any]:
    records = load_records(path)
    selected = [record for record in records if record.get("stage") == "C"]
    foundation_digests = {record.get("foundation_digest") for record in selected}
    adapter_digests = {record.get("adapter_digest") for record in selected}
    if len(foundation_digests) != 1 or SHA256.fullmatch(str(next(iter(foundation_digests)))) is None:
        raise ValueError(f"{suite} foundation digest is invalid or inconsistent")
    if len(adapter_digests) != 1 or SHA256.fullmatch(str(next(iter(adapter_digests)))) is None:
        raise ValueError(f"{suite} adapter digest is invalid or inconsistent")
    results = [validate_review_record(record, suite) for record in selected]
    return {
        "foundation_digest": next(iter(foundation_digests)),
        "adapter_digest": next(iter(adapter_digests)),
        "passed_prompts": sorted(result["prompt_id"] for result in results if result["pass"]),
        "prompts": sorted(results, key=lambda result: result["prompt_id"]),
    }


def evaluate(base_path: Path, heldout_path: Path, required_prompts: tuple[str, ...]) -> dict[str, Any]:
    if (
        len(required_prompts) < MINIMUM_REQUIRED_PROMPTS
        or len(required_prompts) != len(set(required_prompts))
        or any(prompt not in PROMPTS for prompt in required_prompts)
    ):
        raise ValueError(
            f"required prompts must be at least {MINIMUM_REQUIRED_PROMPTS} unique supported prompt IDs"
        )
    base = evaluate_suite(base_path, "base")
    heldout = evaluate_suite(heldout_path, "heldout")
    if base["foundation_digest"] != heldout["foundation_digest"]:
        raise ValueError("foundation digest differs across suites")
    if base["adapter_digest"] != heldout["adapter_digest"]:
        raise ValueError("adapter digest differs across suites")

    supported_intersection = sorted(set(base["passed_prompts"]) & set(heldout["passed_prompts"]))
    required_failures = sorted(set(required_prompts) - set(supported_intersection))
    accepted = not required_failures
    return {
        "schema_version": "1.0.0",
        "status": "PASS" if accepted else "REJECTED",
        "completion_state": "CAPABILITY_SCOPED_COMPLETE" if accepted else "CAPABILITY_SCOPE_REJECTED",
        "profile": PROFILE_NAME,
        "model": {
            "foundation_digest": base["foundation_digest"],
            "adapter_digest": base["adapter_digest"],
        },
        "required_prompts": list(required_prompts),
        "supported_prompt_intersection": supported_intersection,
        "supported_capabilities": [PROMPT_CAPABILITIES[prompt] for prompt in required_prompts],
        "excluded_prompts": sorted(set(PROMPTS) - set(required_prompts)),
        "excluded_capabilities": EXCLUDED_CAPABILITIES,
        "discarded_response_fields": DISCARDED_RESPONSE_FIELDS,
        "required_failures": required_failures,
        "scoped_serving_eligible": accepted,
        "full_patch_capable_promotion_eligible": False,
        "strict_full_gate_overridden": False,
        "suites": {"base": base, "heldout": heldout},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Capability-scoped code-review completion gate")
    parser.add_argument("--base-responses", required=True)
    parser.add_argument("--heldout-responses", required=True)
    parser.add_argument("--required-prompts", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    required_prompts = tuple(item.strip() for item in args.required_prompts.split(",") if item.strip())
    result = evaluate(Path(args.base_responses), Path(args.heldout_responses), required_prompts)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

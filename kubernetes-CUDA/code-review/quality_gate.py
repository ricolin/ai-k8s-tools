from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


PROMPT_REFERENCES = {
    "python-review": ("repo-python", None, "python-unit", "python-diff"),
    "go-review": ("repo-go", None, "go-unit", "go-diff"),
    "rust-review": ("repo-rust", None, "rust-unit", "rust-diff"),
    "bash-review": ("repo-bash", None, "bash-unit", "bash-diff"),
    "yaml-review": ("repo-yaml", None, "yaml-unit", "yaml-diff"),
    "pr-agent-fix": ("repo-agent", "pr-agent", "python-unit", "agent-diff"),
}
PROMPTS = set(PROMPT_REFERENCES)
REVIEW_FIELDS = {"schema_version", "summary", "verdict", "findings", "tests", "unknowns"}
FINDING_FIELDS = {"id", "severity", "category", "path", "line", "evidence", "impact", "recommendation", "test"}
FIX_FIELDS = {"status", "patch_id", "unified_diff", "rationale", "expected_tests"}
PLAN_FIELDS = {"repository_lock_id", "pull_request_lock_id", "tasks"}
TASK_FIELDS = {"id", "tool", "arguments", "timeout_seconds", "cleanup_required"}
TOOL_ARGUMENT_KEYS = {
    "inspect_repository": {"repository_lock_id"},
    "inspect_diff": {"pull_request_lock_id"},
    "apply_candidate_patch": {"patch_id", "repository_lock_id"},
    "run_profile": {"profile_id", "repository_lock_id"},
    "collect_test_results": {"evidence_ids"},
    "export_patch": {"patch_id", "repository_lock_id"},
    "draft_review": {"evidence_ids"},
}


def validate_response_text(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None, ["response is not one JSON object"]
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != {"reviewer_identity", "review", "candidate_fix", "execution_plan"}:
        return value if isinstance(value, dict) else None, ["top-level fields do not match"]
    review = value.get("review")
    if not isinstance(review, dict) or set(review) != REVIEW_FIELDS:
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
            for finding in findings:
                if not isinstance(finding, dict) or set(finding) != FINDING_FIELDS:
                    errors.append("finding fields do not match")
                    break
                if finding.get("severity") not in {"critical", "high", "medium", "low"}:
                    errors.append("finding severity is invalid")
                if finding.get("category") not in {
                    "correctness", "reliability", "security", "compatibility", "performance", "testing", "style"
                }:
                    errors.append("finding category is invalid")
                if not isinstance(finding.get("line"), int) or finding["line"] < 1:
                    errors.append("finding line is invalid")
    fix = value.get("candidate_fix")
    if not isinstance(fix, dict) or set(fix) != FIX_FIELDS:
        errors.append("candidate fix fields do not match")
    elif fix.get("status") == "PROPOSED":
        patch = fix.get("unified_diff")
        if not isinstance(patch, str) or not re.search(r"^diff --git a/.+ b/.+$", patch, flags=re.MULTILINE):
            errors.append("proposed fix is not a unified diff")
        elif any(part in patch for part in ("../", "a/.git/", "b/.git/", "GIT binary patch", "Binary files ")):
            errors.append("proposed fix contains an unsafe path or binary patch")
    elif fix.get("status") not in {"NOT_NEEDED", "BLOCKED"}:
        errors.append("candidate fix status is invalid")
    elif fix.get("patch_id") is not None or fix.get("unified_diff") != "":
        errors.append("non-proposed fix contains patch data")
    plan = value.get("execution_plan")
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        errors.append("execution plan fields do not match")
    else:
        tasks = plan.get("tasks")
        if not isinstance(tasks, list) or len(tasks) > 30:
            errors.append("execution plan task list is invalid")
        else:
            for task in tasks:
                if not isinstance(task, dict) or set(task) != TASK_FIELDS:
                    errors.append("task fields do not match")
                    continue
                tool = task.get("tool")
                arguments = task.get("arguments")
                if tool not in TOOL_ARGUMENT_KEYS:
                    errors.append("task tool is invalid")
                elif not isinstance(arguments, dict) or set(arguments) != TOOL_ARGUMENT_KEYS[tool]:
                    errors.append("task arguments do not match the tool contract")
                if task.get("cleanup_required") is not True:
                    errors.append("task cleanup_required must be true")
                timeout = task.get("timeout_seconds")
                if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
                    errors.append("task timeout is invalid")
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
    repository_lock, pull_request_lock, profile, evidence = PROMPT_REFERENCES[prompt_id]
    if value is not None:
        review = value.get("review")
        if isinstance(review, dict) and isinstance(review.get("findings"), list):
            if any(finding.get("evidence") != evidence for finding in review["findings"] if isinstance(finding, dict)):
                errors.append("finding evidence was not copied from the request")
        plan = value.get("execution_plan")
        if isinstance(plan, dict):
            if plan.get("repository_lock_id") != repository_lock:
                errors.append("repository lock was not copied from the request")
            if plan.get("pull_request_lock_id") != pull_request_lock:
                errors.append("pull-request lock was not copied from the request")
            for task in plan.get("tasks", []) if isinstance(plan.get("tasks"), list) else []:
                arguments = task.get("arguments", {}) if isinstance(task, dict) else {}
                if "repository_lock_id" in arguments and arguments["repository_lock_id"] != repository_lock:
                    errors.append("task repository lock was not copied from the request")
                if "pull_request_lock_id" in arguments and arguments["pull_request_lock_id"] != pull_request_lock:
                    errors.append("task pull-request lock was not copied from the request")
                if "profile_id" in arguments and arguments["profile_id"] != profile:
                    errors.append("task profile was not copied from the request")
                if "evidence_ids" in arguments and arguments["evidence_ids"] != [evidence]:
                    errors.append("task evidence was not copied from the request")
                fix = value.get("candidate_fix", {})
                if "patch_id" in arguments and arguments["patch_id"] != fix.get("patch_id"):
                    errors.append("task patch id does not match candidate fix")
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

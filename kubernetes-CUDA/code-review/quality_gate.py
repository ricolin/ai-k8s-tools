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
STAGES = ("foundation", "A", "B", "C")
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
HUNK_HEADER = re.compile(
    r"^@@ -(?:[0-9]+)(?:,([0-9]+))? \+(?:[0-9]+)(?:,([0-9]+))? @@(?: .*)?$"
)
PATCH_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def unified_diff_hunks_are_valid(patch: str) -> bool:
    """Validate every unified-diff hunk body against its declared line counts."""
    lines = patch.splitlines()
    found_hunk = False
    index = 0
    while index < len(lines):
        match = HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        found_hunk = True
        expected_old = int(match.group(1) or "1")
        expected_new = int(match.group(2) or "1")
        observed_old = 0
        observed_new = 0
        index += 1
        while index < len(lines) and not lines[index].startswith(("@@ ", "diff --git ")):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if line.startswith(" "):
                observed_old += 1
                observed_new += 1
            elif line.startswith("-"):
                observed_old += 1
            elif line.startswith("+"):
                observed_new += 1
            else:
                return False
            index += 1
        if observed_old != expected_old or observed_new != expected_new:
            return False
    return found_hunk


def unified_diff_hunks_change_content(patch: str) -> bool:
    """Require at least one hunk whose old and new bodies differ."""
    lines = patch.splitlines()
    found_hunk = False
    changed = False
    index = 0
    while index < len(lines):
        if HUNK_HEADER.fullmatch(lines[index]) is None:
            index += 1
            continue
        found_hunk = True
        old: list[str] = []
        new: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].startswith(("@@ ", "diff --git ")):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if line.startswith(" "):
                old.append(line[1:])
                new.append(line[1:])
            elif line.startswith("-"):
                old.append(line[1:])
            elif line.startswith("+"):
                new.append(line[1:])
            else:
                return False
            index += 1
        changed = changed or old != new
    return found_hunk and changed


def unified_diff_preimage(patch: str) -> str | None:
    """Return the exact old-side text for a single-hunk, single-file patch."""
    lines = patch.splitlines()
    hunk_indexes = [index for index, line in enumerate(lines) if HUNK_HEADER.fullmatch(line)]
    if len(hunk_indexes) != 1 or len([line for line in lines if line.startswith("diff --git ")]) != 1:
        return None
    result: list[str] = []
    index = hunk_indexes[0] + 1
    while index < len(lines) and not lines[index].startswith(("@@ ", "diff --git ")):
        line = lines[index]
        if line.startswith("\\ No newline at end of file"):
            index += 1
            continue
        if line.startswith((" ", "-")):
            result.append(line[1:])
        elif not line.startswith("+"):
            return None
        index += 1
    return "\n".join(result)


def normalize_response_text(raw: str) -> tuple[str, tuple[str, ...]]:
    if raw.startswith("{%") and raw.endswith("%}"):
        return raw[:1] + raw[2:-2], ("qwen-template-brace-wrapper",)
    return raw, ()


def validate_response_text(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    raw, _ = normalize_response_text(raw)
    try:
        value = json.loads(raw, strict=False)
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
        for field in ("tests", "unknowns"):
            values = review.get(field)
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                errors.append(f"review {field} must be an array of strings")
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
    else:
        expected_tests = fix.get("expected_tests")
        if not isinstance(expected_tests, list) or not all(isinstance(item, str) for item in expected_tests):
            errors.append("candidate fix expected_tests must be an array of strings")
    if isinstance(fix, dict) and set(fix) == FIX_FIELDS and fix.get("status") == "PROPOSED":
        patch_id = fix.get("patch_id")
        if not isinstance(patch_id, str) or PATCH_ID.fullmatch(patch_id) is None:
            errors.append("proposed fix patch id is invalid")
        patch = fix.get("unified_diff")
        if not isinstance(patch, str) or not re.search(r"^diff --git a/.+ b/.+$", patch, flags=re.MULTILINE):
            errors.append("proposed fix is not a unified diff")
        else:
            sections = len(re.findall(r"^diff --git a/.+ b/.+$", patch, flags=re.MULTILINE))
            old_headers = len(re.findall(r"^--- (?:a/.+|/dev/null)$", patch, flags=re.MULTILINE))
            new_headers = len(re.findall(r"^\+\+\+ (?:b/.+|/dev/null)$", patch, flags=re.MULTILINE))
            if sections != old_headers or sections != new_headers:
                errors.append("proposed fix file headers do not match diff sections")
            if any(part in patch for part in ("../", "a/.git/", "b/.git/", "GIT binary patch", "Binary files ")):
                errors.append("proposed fix contains an unsafe path or binary patch")
            if not patch.endswith("\n"):
                errors.append("proposed fix must end with a newline")
            if not unified_diff_hunks_are_valid(patch):
                errors.append("proposed fix hunk line counts do not match headers")
            elif not unified_diff_hunks_change_content(patch):
                errors.append("proposed fix does not change source content")
    elif isinstance(fix, dict) and set(fix) == FIX_FIELDS and fix.get("status") not in {"NOT_NEEDED", "BLOCKED"}:
        errors.append("candidate fix status is invalid")
    elif isinstance(fix, dict) and set(fix) == FIX_FIELDS and (
        fix.get("patch_id") is not None or fix.get("unified_diff") != ""
    ):
        errors.append("non-proposed fix contains patch data")
    plan = value.get("execution_plan")
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        errors.append("execution plan fields do not match")
    else:
        pull_request_lock_id = plan.get("pull_request_lock_id")
        if pull_request_lock_id is not None and not isinstance(pull_request_lock_id, str):
            errors.append("pull-request lock must be a string or null")
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
        expected_finding = record.get("expected_finding")
        if isinstance(expected_finding, dict):
            findings = review.get("findings", []) if isinstance(review, dict) else []
            matched = any(
                isinstance(finding, dict)
                and finding.get("path") == expected_finding.get("path")
                and finding.get("line") == expected_finding.get("line")
                and finding.get("evidence") == expected_finding.get("evidence")
                for finding in findings
            )
            if not matched:
                errors.append("expected held-out finding was not reported")
            if isinstance(review, dict) and review.get("verdict") != "REQUEST_CHANGES":
                errors.append("held-out defect verdict must request changes")
        expected_preimage = record.get("expected_patch_preimage")
        if record.get("stage") == "C" and isinstance(expected_preimage, str):
            fix = value.get("candidate_fix")
            if not isinstance(fix, dict) or fix.get("status") != "PROPOSED":
                errors.append("evaluated defect requires a proposed fix")
            elif unified_diff_preimage(str(fix.get("unified_diff", ""))) != expected_preimage:
                errors.append("proposed fix preimage does not match supplied evidence")
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


def evaluate(path: Path, required_stages: tuple[str, ...] = STAGES) -> dict[str, Any]:
    if (
        not required_stages
        or len(required_stages) != len(set(required_stages))
        or any(stage not in STAGES for stage in required_stages)
        or required_stages != tuple(sorted(required_stages, key=STAGES.index))
        or "B" not in required_stages
        or "C" not in required_stages
    ):
        raise ValueError("quality stages must be an ordered unique subset containing B and C")
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if {record.get("stage") for record in records} != set(required_stages):
        raise ValueError("responses do not contain the exact stage set")
    foundation_digests = {record.get("foundation_digest") for record in records}
    if len(foundation_digests) != 1 or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", str(next(iter(foundation_digests)))
    ):
        raise ValueError("foundation digest differs across responses")
    for prompt_id in sorted(PROMPTS):
        selected = [record for record in records if record.get("prompt_id") == prompt_id]
        if len(selected) != len(required_stages):
            raise ValueError(f"prompt {prompt_id} does not contain exactly one response per stage")
        prompt_digests = {record.get("prompt_digest") for record in selected}
        if len(prompt_digests) != 1 or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(next(iter(prompt_digests)))
        ):
            raise ValueError(f"prompt digest differs across stages: {prompt_id}")
        reviewer_identities = {record.get("expected_reviewer_identity") for record in selected}
        if len(reviewer_identities) != 1:
            raise ValueError(f"reviewer identity differs across stages: {prompt_id}")
    stages: dict[str, Any] = {}
    for stage in required_stages:
        selected = [record for record in records if record.get("stage") == stage]
        prompt_ids = [record.get("prompt_id") for record in selected]
        if len(prompt_ids) != len(PROMPTS) or set(prompt_ids) != PROMPTS:
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
        "evaluated_stages": list(required_stages),
        "stages": stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic code-review A/B/C quality gate")
    parser.add_argument("--responses", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stages", default=",".join(STAGES))
    args = parser.parse_args()
    required_stages = tuple(item.strip() for item in args.stages.split(",") if item.strip())
    result = evaluate(Path(args.responses), required_stages)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

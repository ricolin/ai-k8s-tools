from __future__ import annotations

import json

import pytest

from ai_build_tools_k8s.code_review_agent import (
    evaluate_green,
    make_request,
    parse_intent,
    validate_candidate_fix,
    validate_response,
)
from ai_build_tools_k8s.code_review_model import ContractError


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def release() -> dict:
    return {
        "schema_version": "1.0.0",
        "stage": "C",
        "validation_level": "AUTOMATED_ACCEPTED",
        "foundation_digest": digest("a"),
        "adapter_digest": digest("b"),
        "tokenizer_digest": digest("c"),
        "chat_template_digest": digest("d"),
        "review_schema_digest": digest("e"),
        "agent_plan_schema_digest": digest("f"),
        "policy_profile_digest": digest("1"),
        "serving_model_name": "code-reviewer-c",
        "lora_rank": 16,
        "supported_languages": ["bash", "go", "python", "rust", "yaml"],
        "supported_target_types": ["agent-plan", "pull-request", "repository", "single-file"],
    }


def packet() -> dict:
    return {
        "reference_index": {
            "repository_lock_ids": ["repo-1"],
            "pull_request_lock_ids": ["pr-1"],
            "profile_ids": ["python-unit"],
            "evidence_ids": ["diff-1"],
        }
    }


def response() -> dict:
    return {
        "reviewer_identity": digest("b"),
        "review": {
            "schema_version": "1.0.0",
            "summary": "One defect",
            "verdict": "REQUEST_CHANGES",
            "findings": [
                {
                    "id": "F1",
                    "severity": "high",
                    "category": "correctness",
                    "path": "src/cache.py",
                    "line": 18,
                    "evidence": "diff-1",
                    "impact": "State leaks",
                    "recommendation": "Use None",
                    "test": "Call twice",
                }
            ],
            "tests": ["Call twice"],
            "unknowns": ["Tests not run"],
        },
        "candidate_fix": {
            "status": "PROPOSED",
            "patch_id": "fix-1",
            "unified_diff": "diff --git a/src/cache.py b/src/cache.py\n--- a/src/cache.py\n+++ b/src/cache.py\n@@ -1 +1 @@\n-old\n+new\n",
            "rationale": "Correct the defect",
            "expected_tests": ["Call twice"],
        },
        "execution_plan": {
            "repository_lock_id": "repo-1",
            "pull_request_lock_id": "pr-1",
            "tasks": [
                {
                    "id": "apply",
                    "tool": "apply_candidate_patch",
                    "arguments": {"patch_id": "fix-1", "repository_lock_id": "repo-1"},
                    "timeout_seconds": 120,
                    "cleanup_required": True,
                },
                {
                    "id": "test",
                    "tool": "run_profile",
                    "arguments": {"profile_id": "python-unit", "repository_lock_id": "repo-1"},
                    "timeout_seconds": 1800,
                    "cleanup_required": True,
                },
            ],
        },
    }


def test_response_accepts_bounded_patch_and_profile() -> None:
    assert validate_response(response(), release(), packet()) == response()


def test_response_accepts_grounded_style_finding() -> None:
    value = response()
    value["review"]["findings"][0]["category"] = "style"

    assert validate_response(value, release(), packet()) == value


def test_request_distinguishes_finding_and_evidence_ids() -> None:
    request = make_request(release(), packet())
    payload = json.loads(request["messages"][1]["content"])

    assert payload["contract"]["identifier_rules"] == {
        "finding.id": "reviewer-created label such as F1",
        "finding.evidence": "exact value from review_packet.reference_index.evidence_ids",
    }
    assert "implementation and test paths" in request["messages"][0]["content"]


@pytest.mark.parametrize(
    ("text", "mode", "languages"),
    [
        ("go review https://github.com/ricolin/ai-build-tools/", "review-only", ["bash", "go", "python", "rust", "yaml"]),
        ("go review https://github.com/ricolin/ai-build-tools/ on the bash scripts", "review-only", ["bash"]),
        (
            "go review https://github.com/ricolin/ai-build-tools/ and provide fix until all your review green",
            "fix-until-green",
            ["bash", "go", "python", "rust", "yaml"],
        ),
        (
            "go review https://github.com/ricolin/ai-build-tools/ on the bash scripts and provide fix until all your review green",
            "fix-until-green",
            ["bash"],
        ),
    ],
)
def test_parse_intent_supports_simple_review_commands(text: str, mode: str, languages: list[str]) -> None:
    intent = parse_intent(text)

    assert intent["repository"] == "https://github.com/ricolin/ai-build-tools.git"
    assert intent["mode"] == mode
    assert intent["scope"]["languages"] == languages
    assert intent["publish"] is False
    assert intent["retain_resources"] is True
    assert intent["target_type"] == "repository"
    assert intent["pull_request_number"] is None


def test_parse_intent_supports_github_pull_request() -> None:
    intent = parse_intent(
        "go review https://github.com/ricolin/ai-build-tools/pull/42 on the python and yaml files "
        "and provide fix until all reviews are green"
    )

    assert intent["repository"] == "https://github.com/ricolin/ai-build-tools.git"
    assert intent["target_type"] == "pull_request"
    assert intent["pull_request_number"] == 42
    assert intent["scope"]["languages"] == ["python", "yaml"]
    assert intent["mode"] == "fix-until-green"


def test_parse_intent_rejects_unsupported_target() -> None:
    with pytest.raises(ContractError, match="unsupported review request"):
        parse_intent("go review https://example.com/ricolin/ai-build-tools")


def test_evaluate_green_requires_tests_and_clean_final_review() -> None:
    final = response()
    final["review"]["verdict"] = "APPROVE"
    final["review"]["findings"] = []
    final["candidate_fix"] = {
        "status": "NOT_NEEDED",
        "patch_id": None,
        "unified_diff": "",
        "rationale": "No remaining findings",
        "expected_tests": ["python-unit"],
    }

    result = evaluate_green(final, "UNIT_TEST_STATUS=0\nSOURCE_COMMIT=" + ("a" * 40) + "\n")

    assert result["status"] == "GREEN"
    assert all(result["checks"].values())


def test_response_rejects_path_escape_and_unknown_profile() -> None:
    invalid = response()["candidate_fix"]
    invalid["unified_diff"] = "diff --git a/../secret b/../secret\n--- a/../secret\n+++ b/../secret\n"
    with pytest.raises(ContractError, match="escapes"):
        validate_candidate_fix(invalid)

    invalid_response = response()
    invalid_response["execution_plan"]["tasks"][1]["arguments"]["profile_id"] = "invented"
    with pytest.raises(ContractError, match="ungrounded profile"):
        validate_response(invalid_response, release(), packet())


def test_candidate_fix_rejects_missing_terminal_newline() -> None:
    invalid = response()["candidate_fix"]
    invalid["unified_diff"] = invalid["unified_diff"].rstrip("\n")

    with pytest.raises(ContractError, match="must end with a newline"):
        validate_candidate_fix(invalid)


@pytest.mark.parametrize("field", ["tests", "unknowns"])
def test_response_rejects_invalid_review_list_items(field: str) -> None:
    invalid = response()
    invalid["review"][field] = [""]

    with pytest.raises(ContractError, match=f"review {field} contain"):
        validate_response(invalid, release(), packet())

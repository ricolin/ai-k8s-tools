from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from ai_build_tools_k8s.code_review_agent import (
    create_followup_packet,
    collect_packet,
    evaluate_green,
    make_request,
    parse_intent,
    run,
    validate_candidate_fix,
    validate_review_capability_contract,
    validate_response,
    validate_scoped_review_response,
)
from ai_build_tools_k8s.code_review_model import ContractError


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def release() -> dict:
    return {
        "schema_version": "1.0.0",
        "stage": "C",
        "validation_level": "AUTOMATED_REJECTED",
        "foundation_digest": digest("a"),
        "adapter_digest": digest("b"),
        "tokenizer_digest": digest("c"),
        "chat_template_digest": digest("d"),
        "review_schema_digest": digest("e"),
        "agent_plan_schema_digest": digest("f"),
        "policy_profile_digest": digest("1"),
        "serving_model_name": "code-reviewer-c-candidate",
        "promotion_state": "QUALITY_REJECTED",
        "quality_status": "FAIL",
        "promotion_blocked": True,
        "parent_release_digest": digest("2"),
        "promotion_history": [
            {"state": "TRAINING_COMPLETE", "evidence_digest": digest("3")},
            {"state": "WORKFLOW_VALIDATED", "evidence_digest": digest("4")},
            {"state": "QUALITY_REJECTED", "evidence_digest": digest("5")},
        ],
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


def capability_contract() -> dict:
    return {
        "schema_version": "1.0.0",
        "status": "PASS",
        "completion_state": "CAPABILITY_SCOPED_COMPLETE",
        "profile": "grounded-review-v1",
        "model": {
            "foundation_digest": digest("a"),
            "adapter_digest": digest("b"),
        },
        "required_prompts": ["go-review", "rust-review", "yaml-review", "pr-agent-fix"],
        "supported_prompt_intersection": [
            "go-review",
            "pr-agent-fix",
            "rust-review",
            "yaml-review",
        ],
        "excluded_prompts": ["bash-review", "python-review"],
        "excluded_capabilities": [
            "candidate-patch-generation",
            "candidate-patch-application",
            "execution-plan-consumption",
            "fix-until-green",
        ],
        "discarded_response_fields": ["candidate_fix", "execution_plan"],
        "required_failures": [],
        "scoped_serving_eligible": True,
        "full_patch_capable_promotion_eligible": False,
        "strict_full_gate_overridden": False,
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


def green_response() -> dict:
    value = response()
    value["review"]["verdict"] = "APPROVE"
    value["review"]["findings"] = []
    value["candidate_fix"] = {
        "status": "NOT_NEEDED",
        "patch_id": None,
        "unified_diff": "",
        "rationale": "No remaining findings",
        "expected_tests": [],
    }
    value["execution_plan"]["tasks"] = [
        {
            "id": "collect",
            "tool": "collect_test_results",
            "arguments": {"evidence_ids": ["diff-1"]},
            "timeout_seconds": 60,
            "cleanup_required": True,
        },
        {
            "id": "review",
            "tool": "draft_review",
            "arguments": {"evidence_ids": ["diff-1"]},
            "timeout_seconds": 60,
            "cleanup_required": True,
        },
    ]
    return value


def test_response_accepts_bounded_patch_and_profile() -> None:
    assert validate_response(response(), release(), packet()) == response()


def test_response_accepts_grounded_style_finding() -> None:
    value = response()
    value["review"]["findings"][0]["category"] = "style"

    assert validate_response(value, release(), packet()) == value


def test_run_preserves_invalid_response_before_contract_validation(tmp_path: Path) -> None:
    release_path = tmp_path / "release.json"
    packet_path = tmp_path / "packet.json"
    fixture_path = tmp_path / "fixture.json"
    output = tmp_path / "output"
    invalid = response()
    invalid["review"]["findings"][0]["test"] = ""
    for path, value in (
        (release_path, release()),
        (packet_path, packet()),
        (fixture_path, invalid),
    ):
        path.write_text(json.dumps(value))

    with pytest.raises(ContractError, match="finding test is required"):
        run(release_path, packet_path, output, "", fixture_path, 10)

    assert json.loads((output / "response.unvalidated.json").read_text()) == invalid
    assert not (output / "response.json").exists()
    assert not (output / "run.json").exists()


def test_scoped_run_discards_invalid_patch_and_plan(tmp_path: Path) -> None:
    release_path = tmp_path / "release.json"
    packet_path = tmp_path / "packet.json"
    fixture_path = tmp_path / "fixture.json"
    contract_path = tmp_path / "capability.json"
    output = tmp_path / "output"
    scoped = response()
    scoped["candidate_fix"] = {"unsafe": "discarded"}
    scoped["execution_plan"] = {"unsafe": "discarded"}
    for path, value in (
        (release_path, release()),
        (packet_path, packet()),
        (fixture_path, scoped),
        (contract_path, capability_contract()),
    ):
        path.write_text(json.dumps(value))

    run(release_path, packet_path, output, "", fixture_path, 10, contract_path)

    assert json.loads((output / "response.unvalidated.json").read_text()) == scoped
    assert json.loads((output / "response.json").read_text()) == {
        "reviewer_identity": scoped["reviewer_identity"],
        "review": scoped["review"],
    }
    run_result = json.loads((output / "run.json").read_text())
    assert run_result["status"] == "CAPABILITY_SCOPED_PASS"
    assert run_result["discarded_response_fields"] == ["candidate_fix", "execution_plan"]


def test_scoped_review_requires_pull_request_capability_and_lock() -> None:
    contract = capability_contract()
    assert validate_review_capability_contract(contract, release()) == contract

    missing_capability = capability_contract()
    missing_capability["required_prompts"].remove("pr-agent-fix")
    with pytest.raises(ContractError, match="does not include pull-request review"):
        validate_review_capability_contract(missing_capability, release())

    repository_packet = packet()
    repository_packet["reference_index"]["pull_request_lock_ids"] = []
    with pytest.raises(ContractError, match="exactly one pull-request lock"):
        validate_scoped_review_response(
            response(),
            release(),
            repository_packet,
            capability_contract(),
        )


def test_request_distinguishes_finding_and_evidence_ids() -> None:
    request = make_request(release(), packet())
    payload = json.loads(request["messages"][1]["content"])

    assert payload["contract"]["identifier_rules"] == {
        "finding.id": "reviewer-created label such as F1",
        "finding.evidence": "exact value from review_packet.reference_index.evidence_ids",
        "candidate_fix.patch_id": "new lowercase slug such as fix-1; not a digest or supplied identifier",
    }
    assert "implementation and test paths" in request["messages"][0]["content"]
    assert "It is not a digest" in request["messages"][0]["content"]
    assert "never emit an unprefixed source line" in request["messages"][0]["content"]
    assert payload["contract"]["unified_diff_rules"] == {
        "body_line_prefixes": {"addition": "+", "context": " ", "deletion": "-"},
        "hunk_header": "@@ -OLD_START[,OLD_COUNT] +NEW_START[,NEW_COUNT] @@",
        "hunk_header_suffix": "forbidden",
        "new_count": "number of context and addition body lines; omit ,1",
        "old_count": "number of context and deletion body lines; omit ,1",
        "preimage": (
            "context and deletion text after removing its one-character prefix must reproduce "
            "supplied source evidence exactly and in order"
        ),
        "single_line_replacement": (
            "use @@ -LINE +LINE @@ followed by -exact-source and +replacement; use "
            "+LINE,NEW_COUNT when the replacement has multiple lines"
        ),
        "terminal_newline": "required",
    }
    assert payload["contract"]["enum_rules"]["finding.category"] == [
        "correctness",
        "reliability",
        "security",
        "compatibility",
        "performance",
        "testing",
        "style",
    ]


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
    final = green_response()

    value = packet()
    value["source"] = {"commit": "a" * 40}
    value["observed"] = {
        "patch_digest": "sha256:" + ("b" * 64),
        "profile_id": "python-unit",
    }
    result = evaluate_green(
        final,
        "UNIT_TEST_STATUS=0\nSOURCE_COMMIT=" + ("a" * 40)
        + "\nPATCH_SHA256=sha256:" + ("b" * 64) + "\nPROFILE_ID=python-unit\n",
        release(),
        value,
    )

    assert result["status"] == "GREEN"
    assert all(result["checks"].values())


def test_followup_packet_records_observed_patch_and_test_result() -> None:
    source_commit = "a" * 40
    initial = packet()
    initial.update(
        {
            "source": {"commit": source_commit},
            "evidence": [{"id": "diff-1", "kind": "source-file", "content": "old"}],
        }
    )
    patch = response()["candidate_fix"]["unified_diff"]
    result = create_followup_packet(
        initial,
        response(),
        release(),
        f"UNIT_TEST_STATUS=0\nSOURCE_COMMIT={source_commit}\n"
        f"PATCH_SHA256=sha256:{hashlib.sha256(patch.encode()).hexdigest()}\n"
        "PROFILE_ID=python-unit\n",
        "1 passed\n",
        patch,
        1,
    )

    assert result["observed"]["unit_test_status"] == 0
    assert result["observed"]["profile_id"] == "python-unit"
    assert result["instruction"].startswith("Perform the final review")
    assert len(result["reference_index"]["evidence_ids"]) == 4
    assert result["previous_response_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    ("result_line", "message"),
    [
        ("PATCH_SHA256=sha256:" + ("0" * 64), "patch digest"),
        ("PROFILE_ID=wrong-profile", "profile differs"),
    ],
)
def test_followup_packet_rejects_mismatched_sandbox_evidence(result_line: str, message: str) -> None:
    source_commit = "a" * 40
    initial = packet()
    initial.update(
        {
            "source": {"commit": source_commit},
            "evidence": [{"id": "diff-1", "kind": "source-file", "content": "old"}],
        }
    )
    patch = response()["candidate_fix"]["unified_diff"]
    result_env = (
        f"UNIT_TEST_STATUS=0\nSOURCE_COMMIT={source_commit}\n"
        f"PATCH_SHA256=sha256:{hashlib.sha256(patch.encode()).hexdigest()}\n"
        "PROFILE_ID=python-unit\n"
    )
    if result_line.startswith("PATCH_SHA256="):
        result_env = re.sub(r"^PATCH_SHA256=.*$", result_line, result_env, flags=re.MULTILINE)
    else:
        result_env = re.sub(r"^PROFILE_ID=.*$", result_line, result_env, flags=re.MULTILINE)

    with pytest.raises(ContractError, match=message):
        create_followup_packet(initial, response(), release(), result_env, "1 passed\n", patch, 1)


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


def test_candidate_fix_rejects_mismatched_hunk_line_counts() -> None:
    invalid = response()["candidate_fix"]
    invalid["unified_diff"] = invalid["unified_diff"].replace("@@ -1 +1 @@", "@@ -1,2 +1,2 @@")

    with pytest.raises(ContractError, match="hunk line counts"):
        validate_candidate_fix(invalid)


def test_candidate_fix_rejects_noop_patch() -> None:
    invalid = response()["candidate_fix"]
    invalid["unified_diff"] = invalid["unified_diff"].replace("+new", "+old")

    with pytest.raises(ContractError, match="does not change source content"):
        validate_candidate_fix(invalid)


@pytest.mark.parametrize("field", ["tests", "unknowns"])
def test_response_rejects_invalid_review_list_items(field: str) -> None:
    invalid = response()
    invalid["review"][field] = [""]

    with pytest.raises(ContractError, match=f"review {field} contain"):
        validate_response(invalid, release(), packet())


def test_collect_packet_locks_clean_checkout_and_language_scope(tmp_path: Path) -> None:
    checkout = tmp_path / "source"
    checkout.mkdir()
    (checkout / "scripts").mkdir()
    (checkout / "scripts/check.sh").write_text("#!/bin/sh\nprintf '%s\\n' ok\n")
    (checkout / "ignored.py").write_text("print('not in bash scope')\n")
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(checkout),
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    intent = parse_intent("go review https://github.com/ricolin/ai-build-tools/ on the bash scripts")
    source_lock = {
        "id": f"repo-{commit[:12]}",
        "repository": intent["repository"],
        "commit": commit,
    }

    result = collect_packet(intent, source_lock, release(), "bash-unit", checkout)

    assert result["source"]["commit"] == commit
    assert result["source"]["requested_languages"] == ["bash"]
    assert [item["path"] for item in result["evidence"]] == ["scripts/check.sh"]
    assert result["reference_index"]["profile_ids"] == ["bash-unit"]

    (checkout / "scripts/check.sh").write_text("dirty\n")
    with pytest.raises(ContractError, match="checkout must be clean"):
        collect_packet(intent, source_lock, release(), "bash-unit", checkout)


def test_collect_packet_preserves_utf8_byte_limits(tmp_path: Path) -> None:
    checkout = tmp_path / "source"
    checkout.mkdir()
    (checkout / "example.py").write_text("value = '" + ("水" * 200) + "'\n")
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(checkout), "-c", "user.name=Test", "-c",
            "user.email=test@example.com", "commit", "-m", "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    intent = parse_intent("go review https://github.com/ricolin/ai-build-tools/ on the python files")
    source_lock = {
        "id": f"repo-{commit[:12]}",
        "repository": intent["repository"],
        "commit": commit,
    }

    result = collect_packet(
        intent,
        source_lock,
        release(),
        "python-unit",
        checkout,
        max_file_bytes=256,
        max_total_bytes=256,
    )

    assert result["source"]["content_bytes"] <= 256
    assert result["evidence"][0]["truncated"] is True
    assert result["evidence"][0]["content"].encode("utf-8").decode("utf-8")


def test_collect_packet_selects_exact_tracked_paths(tmp_path: Path) -> None:
    checkout = tmp_path / "source"
    (checkout / "src").mkdir(parents=True)
    (checkout / "tests").mkdir()
    (checkout / "src/first.py").write_text("FIRST = True\n")
    (checkout / "src/target.py").write_text("TARGET = True\n")
    (checkout / "tests/test_target.py").write_text("def test_target(): pass\n")
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(checkout), "-c", "user.name=Test", "-c",
            "user.email=test@example.com", "commit", "-m", "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    intent = parse_intent("go review https://github.com/ricolin/ai-build-tools/ on the python files")
    source_lock = {
        "id": f"repo-{commit[:12]}",
        "repository": intent["repository"],
        "commit": commit,
    }

    result = collect_packet(
        intent,
        source_lock,
        release(),
        "python-unit",
        checkout,
        include_paths=["src/target.py", "tests/test_target.py"],
    )

    assert result["source"]["requested_paths"] == ["src/target.py", "tests/test_target.py"]
    assert [item["path"] for item in result["evidence"]] == ["src/target.py", "tests/test_target.py"]

    with pytest.raises(ContractError, match="include path is not tracked"):
        collect_packet(
            intent,
            source_lock,
            release(),
            "python-unit",
            checkout,
            include_paths=["src/missing.py"],
        )


def test_green_gate_rejects_mismatched_source_lock() -> None:
    final = green_response()
    value = packet()
    value["source"] = {"commit": "a" * 40}
    value["observed"] = {
        "patch_digest": "sha256:" + ("c" * 64),
        "profile_id": "python-unit",
    }

    result = evaluate_green(
        final,
        "UNIT_TEST_STATUS=0\nSOURCE_COMMIT=" + ("b" * 40)
        + "\nPATCH_SHA256=sha256:" + ("c" * 64) + "\nPROFILE_ID=python-unit\n",
        release(),
        value,
    )

    assert result["status"] == "CONTINUE"
    assert result["checks"]["source_commit_matches_lock"] is False


def test_packet_rejects_evidence_index_drift() -> None:
    value = packet()
    value["evidence"] = [{"id": "different-evidence"}]

    with pytest.raises(ContractError, match="evidence index does not match"):
        make_request(release(), value)

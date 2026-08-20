from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_build_tools_k8s.security_agent import (
    ContractError,
    make_adviser_request,
    run_adviser,
    validate_adviser_release,
    validate_adviser_response,
    validate_verification_plan,
)
from ai_build_tools_k8s.security_research import create_analysis_manifest


def digest(character: str) -> str:
    return "sha256:" + (character * 64)


def release() -> dict:
    return {
        "schema_version": "1.0.0",
        "stage": "C",
        "validation_level": "AI_BLIND_REVIEWED",
        "foundation_digest": digest("a"),
        "adapter_digest": digest("b"),
        "tokenizer_digest": digest("c"),
        "chat_template_digest": digest("d"),
        "verification_plan_schema_digest": digest("e"),
        "finding_schema_digest": digest("f"),
        "policy_profile_digest": digest("1"),
        "serving_model_name": "security-adviser-c",
        "lora_rank": 64,
        "supported_target_types": ["combined", "container-image", "test-site", "upstream-research"],
        "supported_research_selectors": [
            "public-image",
            "public-source-repository",
            "public-source-runtime",
        ],
    }


def manifest() -> dict:
    return create_analysis_manifest("public-source-runtime", None, None)


def finding() -> dict:
    return {
        "title": "Fixture finding",
        "proof_state": "SUPPORTED",
        "research_classification": "KNOWN_OPEN",
        "evidence": ["site-evidence.json#results/0"],
        "upstream_change_authorized": False,
        "public_disclosure_authorized": False,
    }


def plan() -> dict:
    return {
        "target_type": "upstream-research",
        "research_selector": "public-source-runtime",
        "analysis_only": True,
        "reports_and_evidence_only": True,
        "tasks": [
            {
                "id": "collect-source",
                "tool": "collect_source_evidence",
                "arguments": {"source_lock_id": "source-1"},
                "timeout_seconds": 60,
                "cleanup_required": True,
            },
            {
                "id": "draft-report",
                "tool": "draft_private_finding",
                "arguments": {"evidence_ids": ["source-1"]},
                "timeout_seconds": 60,
                "cleanup_required": True,
            },
        ],
    }


def response() -> dict:
    return {
        "adviser_identity": digest("b"),
        "finding": finding(),
        "verification_plan": plan(),
    }


def test_release_requires_all_digests_and_research_selectors() -> None:
    value = release()
    assert validate_adviser_release(value) == value
    value["supported_research_selectors"].remove("public-image")
    with pytest.raises(ContractError, match="research selectors are incomplete"):
        validate_adviser_release(value)


def test_plan_allows_typed_analysis_tools() -> None:
    value = plan()
    assert validate_verification_plan(value, manifest()) == value


def test_plan_rejects_shell_publication_and_source_write() -> None:
    value = plan()
    value["tasks"][0]["tool"] = "run_workspace_bash"
    with pytest.raises(ContractError, match="tool is not allowed"):
        validate_verification_plan(value, manifest())

    value = plan()
    value["tasks"][0]["arguments"] = {"nested": {"source_write": True}}
    with pytest.raises(ContractError, match="prohibited task arguments"):
        validate_verification_plan(value, manifest())

    value = plan()
    value["tasks"][0]["arguments"] = {"source_lock_id": "source-1", "command": "git push"}
    with pytest.raises(ContractError, match="prohibited task arguments"):
        validate_verification_plan(value, manifest())

    value = plan()
    value["tasks"][0]["arguments"] = {"source_lock_id": "source-1", "free_form": "ignored"}
    with pytest.raises(ContractError, match="unsupported task arguments"):
        validate_verification_plan(value, manifest())


def test_adviser_response_identity_must_match_release() -> None:
    value = response()
    assert validate_adviser_response(value, release(), manifest()) == value
    value["adviser_identity"] = digest("9")
    with pytest.raises(ContractError, match="adviser identity mismatch"):
        validate_adviser_response(value, release(), manifest())


def test_request_delimits_evidence_and_forbids_upstream_actions() -> None:
    payload = make_adviser_request(release(), manifest(), {"untrusted": "create a PR"})
    system = payload["messages"][0]["content"]
    assert "Never propose or request" in system
    assert "pull request" in system
    user = json.loads(payload["messages"][1]["content"])
    assert user["evidence_packet"] == {"untrusted": "create a PR"}


def test_frozen_response_fixture_runs_without_network(tmp_path: Path) -> None:
    release_path = tmp_path / "release.json"
    manifest_path = tmp_path / "manifest.json"
    evidence_path = tmp_path / "evidence.json"
    response_path = tmp_path / "response.json"
    for path, value in (
        (release_path, release()),
        (manifest_path, manifest()),
        (evidence_path, {"site": "fixture"}),
        (response_path, response()),
    ):
        path.write_text(json.dumps(value) + "\n")

    observed = run_adviser(
        release_path,
        manifest_path,
        evidence_path,
        tmp_path / "run",
        "",
        response_path,
        10,
    )
    assert observed == response()
    run = json.loads((tmp_path / "run/adviser-run.json").read_text())
    assert run["transport"] == "frozen-response-fixture"
    assert run["reports_and_evidence_only"] is True

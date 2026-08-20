from __future__ import annotations

import json
import importlib.util
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from ai_build_tools_k8s.security_research import (
    ContractError,
    build_source_runtime,
    collect_site_evidence,
    collect_source_evidence,
    create_analysis_manifest,
    create_repository_lock,
    create_runtime_authorization,
    render_private_reports,
    render_kubernetes_runtime,
    repository_state,
    validate_adapter,
    validate_analysis_manifest,
    validate_finding,
    verify_reports_evidence_only,
    verify_repository_unchanged,
)


def git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_source(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "checkout", "-b", "main")
    git(path, "remote", "add", "origin", "https://example.invalid/project.git")
    (path / "Dockerfile").write_text("FROM scratch\nCOPY index.html /index.html\n")
    (path / "index.html").write_text("safe fixture\n")
    (path / "requirements.txt").write_text("example==1.0 --hash=sha256:abc\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "fixture")
    return path


def make_adapter(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "kind": "source-runtime",
                "name": "fixture-dockerfile-site",
                "build": {
                    "engine": "docker-buildx-oci",
                    "network": "none",
                    "platform": "linux/amd64",
                    "dockerfile": "Dockerfile",
                    "context": ".",
                },
                "runtime": {
                    "container_port": 8080,
                    "health_path": "/healthz",
                    "run_as_non_root": True,
                    "read_only_root_filesystem": True,
                    "allowed_paths": ["/", "/healthz"],
                    "synthetic_checks": [
                        {
                            "id": "health",
                            "method": "GET",
                            "path": "/healthz",
                            "identity": "anonymous",
                            "expected_status": 200,
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    return path


def make_finding(classification: str = "IMAGE_BUILD") -> dict:
    return {
        "title": "Fixture finding",
        "proof_state": "SUPPORTED",
        "research_classification": classification,
        "confidence": "HIGH",
        "observation": "A deterministic fixture observation.",
        "evidence": ["source-evidence.json#files/0"],
        "likely_owner": "fixture/image-builder",
        "remediation_recommendation": "Review the declared runtime user.",
        "regression_test_recommendation": "Re-run the identity assertion.",
        "upstream_change_authorized": False,
        "public_disclosure_authorized": False,
    }


def test_analysis_manifest_is_fail_closed() -> None:
    manifest = create_analysis_manifest("public-source-runtime", None, None)
    assert validate_analysis_manifest(manifest) == manifest
    manifest["allow_patch_output"] = True
    with pytest.raises(ContractError, match="allow_patch_output must be false"):
        validate_analysis_manifest(manifest)


def test_repository_lock_and_source_evidence(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    lock_path = tmp_path / "repository-lock.json"
    lock = create_repository_lock(
        source,
        "https://example.invalid/project.git",
        "main",
        lock_path,
    )
    assert lock["commit"] == git(source, "rev-parse", "HEAD")
    evidence = collect_source_evidence(source, lock)
    assert evidence["file_count"] == 3
    assert [record["path"] for record in evidence["dependency_locks"]] == [
        "requirements.txt"
    ]


def test_repository_change_is_rejected(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    before = repository_state(source)
    (source / "index.html").write_text("changed\n")
    with pytest.raises(ContractError, match="repository changed"):
        verify_repository_unchanged(source, before)


def test_adapter_requires_offline_build_and_restricted_runtime(tmp_path: Path) -> None:
    adapter_path = make_adapter(tmp_path / "adapter.json")
    adapter = json.loads(adapter_path.read_text())
    assert validate_adapter(adapter)["build"]["network"] == "none"
    adapter["build"]["network"] = "host"
    with pytest.raises(ContractError, match="build network must be none"):
        validate_adapter(adapter)


def test_build_plan_does_not_modify_source(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    lock = create_repository_lock(
        source,
        "https://example.invalid/project.git",
        "main",
        tmp_path / "repository-lock.json",
    )
    result = build_source_runtime(
        source,
        lock,
        make_adapter(tmp_path / "adapter.json"),
        tmp_path / "build",
        execute=False,
    )
    assert result["status"] == "PLANNED"
    assert result["source_tree_modified"] is False
    command = json.loads((tmp_path / "build/build-command.json").read_text())["argv"]
    assert command[0:3] == ["docker", "buildx", "build"]
    assert command[command.index("--network") + 1] == "none"
    assert repository_state(source)["clean"] is True


def test_runtime_authorization_binds_digest_and_forbids_publication() -> None:
    authorization = create_runtime_authorization(
        {"commit": "a" * 40},
        {"image": "registry.example/project@sha256:" + ("b" * 64)},
        {
            "origin": "https://project.test.invalid",
            "addresses": ["192.0.2.10"],
            "namespace": "research",
            "community_operated": False,
            "allowed_paths": ["/healthz"],
            "synthetic_checks": [
                {
                    "id": "health",
                    "method": "GET",
                    "path": "/healthz",
                    "identity": "anonymous",
                    "expected_status": 200,
                }
            ],
        },
        "passive",
        100,
        2,
        1,
        60,
    )
    assert authorization["runtime"]["same_origin_only"] is True
    assert authorization["allow_public_disclosure"] is False
    assert "publication" in authorization["prohibited_operations"]


def test_kubernetes_runtime_is_restricted_and_digest_pinned(tmp_path: Path) -> None:
    adapter = json.loads(make_adapter(tmp_path / "adapter.json").read_text())
    image = "registry.example/project@sha256:" + ("c" * 64)
    manifests, inventory = render_kubernetes_runtime(
        {"image": image},
        adapter,
        "research-run",
        "fixture-site",
        "run-1",
    )
    deployment = next(item for item in manifests if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert container["image"] == image
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert inventory["community_operated"] is False


def test_site_collector_proves_only_synthetic_privilege_boundary(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[2] / "security/fixtures/python-site/site.py"
    spec = importlib.util.spec_from_file_location("security_fixture_site", fixture)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter_path = Path(__file__).resolve().parents[2] / (
            "security/adapters/python-fixture-site-v1.json"
        )
        adapter = json.loads(adapter_path.read_text())
        port = server.server_address[1]
        inventory = {
            "origin": f"http://127.0.0.1:{port}",
            "addresses": ["127.0.0.1"],
            "namespace": "local-fixture",
            "community_operated": False,
            "allowed_paths": adapter["runtime"]["allowed_paths"],
            "synthetic_checks": adapter["runtime"]["synthetic_checks"],
        }
        authorization = create_runtime_authorization(
            {"commit": "a" * 40},
            {"image": "registry.example/fixture@sha256:" + ("b" * 64)},
            inventory,
            "active-safe-canary",
            20,
            5,
            1,
            30,
        )
        evidence = collect_site_evidence(authorization, tmp_path / "site-evidence")
        assert evidence["off_origin_requests"] == 0
        assert evidence["request_count"] == 3
        assert evidence["proofs"] == [
            {
                "state": "PROVEN_SYNTHETIC_PRIVILEGE_BOUNDARY",
                "check_id": "viewer-admin-canary",
                "identity": "viewer-fixture",
                "expected_status": 403,
                "observed_status": 200,
                "canary_response_digest": "sha256:" + module.CANARY_DIGEST,
                "real_host_root_shell": False,
                "credential_retained": False,
                "persistence_created": False,
            }
        ]
    finally:
        server.shutdown()
        thread.join()


def test_potential_novelty_requires_two_independent_reproductions() -> None:
    finding = make_finding("POTENTIALLY_NOVEL")
    finding["novelty_gate"] = {
        "independent_reproductions": [{"environment_id": "one"}],
        "positive_control": True,
        "negative_control": True,
        "affected_bounds": ["v1"],
        "unaffected_bounds": ["v2"],
        "source_location": "src/app.py:10",
        "root_cause_evidence": ["evidence/root-cause.json"],
        "known_work_search": {"complete": True},
    }
    with pytest.raises(ContractError, match="two reproductions"):
        validate_finding(finding)
    finding["novelty_gate"]["independent_reproductions"].append(
        {"environment_id": "two"}
    )
    assert validate_finding(finding) == finding


def test_reports_only_rejects_patch_and_issue_artifacts(tmp_path: Path) -> None:
    assessment = tmp_path / "assessment"
    render_private_reports(make_finding(), assessment)
    report = verify_reports_evidence_only(assessment)
    assert report["pass"] is True

    (assessment / "changes.patch").write_text("not allowed\n")
    with pytest.raises(ContractError, match="prohibited patch/issue/PR"):
        verify_reports_evidence_only(assessment)


def test_reports_only_rejects_missing_required_report(tmp_path: Path) -> None:
    assessment = tmp_path / "assessment"
    render_private_reports(make_finding(), assessment)
    (assessment / "remediation-recommendations.md").unlink()
    with pytest.raises(ContractError, match="missing required reports"):
        verify_reports_evidence_only(assessment)

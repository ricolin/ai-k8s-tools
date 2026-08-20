from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ai_build_tools_k8s.workflow import canonical_json, sha256_file, write_json


SCHEMA_VERSION = "1.0.0"
TARGET_TYPE = "upstream-research"
RESEARCH_SELECTORS = {
    "public-image",
    "public-source-repository",
    "public-source-runtime",
}
PROOF_STATES = {
    "PROVEN",
    "SUPPORTED",
    "UNVERIFIED",
    "NOT_REPRODUCED",
    "BLOCKED_BY_POLICY",
}
RESEARCH_CLASSIFICATIONS = {
    "KNOWN_FIXED",
    "KNOWN_OPEN",
    "DUPLICATE",
    "DOWNSTREAM_CONFIG",
    "IMAGE_BUILD",
    "MCAPI_DRIVER",
    "MAGNUM_SERVICE",
    "UBUNTU_PACKAGING",
    "KUBERNETES_COMPATIBILITY",
    "UNKNOWN_MULTI_REPOSITORY",
    "POTENTIALLY_NOVEL",
    "INSUFFICIENT_EVIDENCE",
}
FALSE_POLICY_FIELDS = {
    "allow_source_write",
    "allow_patch_output",
    "allow_git_commit",
    "allow_git_push",
    "allow_issue_create",
    "allow_pr_create",
    "allow_issue_pr_artifacts",
    "allow_upstream_comment",
    "allow_external_live_scan",
    "allow_public_disclosure",
}
PROHIBITED_REPORT_NAMES = {
    "changes.patch",
    "changes.diff",
    "issue.md",
    "pull-request.md",
    "pull_request.md",
    "pr.md",
}
PROHIBITED_REPORT_SUFFIXES = {".patch", ".diff"}
DEPENDENCY_LOCK_NAMES = {
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}


class ContractError(ValueError):
    pass


def _run_git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _require_sha256(value: str, field: str) -> None:
    _require(
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:]),
        f"{field} must be a lowercase sha256 digest",
    )


def _require_commit(value: str, field: str = "commit") -> None:
    _require(
        len(value) == 40 and all(character in "0123456789abcdef" for character in value),
        f"{field} must be a 40-character lowercase Git commit",
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    _require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def _relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    _require(not path.is_absolute(), f"{field} must be relative")
    _require(".." not in path.parts, f"{field} cannot escape the source tree")
    _require(value not in {"", "."}, f"{field} cannot be empty")
    return path.as_posix()


def repository_state(source: Path) -> dict[str, Any]:
    source = source.resolve()
    _require((source / ".git").exists(), f"not a Git repository: {source}")
    commit = _run_git(source, "rev-parse", "HEAD")
    _require_commit(commit)
    status = _run_git(source, "status", "--porcelain=v2", "--branch")
    dirty = [line for line in status.splitlines() if line and not line.startswith("#")]
    remotes = sorted(_run_git(source, "remote", "-v").splitlines())
    refs = sorted(_run_git(source, "show-ref").splitlines())
    submodules = _run_git(source, "submodule", "status", "--recursive").splitlines()
    return {
        "schema_version": SCHEMA_VERSION,
        "source": str(source),
        "commit": commit,
        "clean": not dirty,
        "dirty_records": dirty,
        "status": status.splitlines(),
        "remotes": remotes,
        "refs_sha256": f"sha256:{hashlib.sha256(canonical_json(refs)).hexdigest()}",
        "submodules": submodules,
    }


def create_repository_lock(
    source: Path,
    repository: str,
    requested_ref: str,
    output: Path,
) -> dict[str, Any]:
    state = repository_state(source)
    _require(state["clean"], "source repository must be clean before locking")
    parsed = urlparse(repository)
    _require(parsed.scheme == "https" and parsed.netloc, "repository must be a public HTTPS URL")
    lock = {
        "schema_version": SCHEMA_VERSION,
        "project": source.name,
        "repository": repository,
        "requested_ref": requested_ref,
        "commit": state["commit"],
        "submodules": state["submodules"],
        "repository_state": state,
    }
    write_json(output, lock)
    return lock


def verify_repository_unchanged(source: Path, before: dict[str, Any]) -> dict[str, Any]:
    after = repository_state(source)
    fields = ("commit", "clean", "dirty_records", "remotes", "refs_sha256", "submodules")
    mismatches = [field for field in fields if before.get(field) != after.get(field)]
    if mismatches:
        raise ContractError(f"repository changed: {', '.join(mismatches)}")
    return after


def validate_analysis_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    _require(manifest.get("target_type") == TARGET_TYPE, "target_type must be upstream-research")
    _require(manifest.get("research_selector") in RESEARCH_SELECTORS, "invalid research_selector")
    _require(manifest.get("analysis_only") is True, "analysis_only must be true")
    for field in sorted(FALSE_POLICY_FIELDS):
        _require(field in manifest, f"missing analysis-only field: {field}")
        _require(manifest[field] is False, f"{field} must be false")
    return manifest


def create_analysis_manifest(
    selector: str,
    source_lock: dict[str, Any] | None,
    target_lock: dict[str, Any] | None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target_type": TARGET_TYPE,
        "research_selector": selector,
        "analysis_only": True,
        **{field: False for field in sorted(FALSE_POLICY_FIELDS)},
        "source_lock": source_lock,
        "target_lock": target_lock,
        "allowed_outputs": [
            "reports",
            "evidence",
            "safe-reproduction",
            "remediation-recommendations",
            "regression-test-recommendations",
        ],
    }
    return validate_analysis_manifest(manifest)


def create_runtime_authorization(
    repository_lock: dict[str, Any],
    artifact_lock: dict[str, Any],
    runtime_inventory: dict[str, Any],
    scan_mode: str,
    max_requests: int,
    requests_per_second: int,
    max_concurrency: int,
    max_seconds: int,
) -> dict[str, Any]:
    _require(scan_mode in {"passive", "active-safe-canary"}, "unsupported scan mode")
    _require(runtime_inventory.get("community_operated") is False, "community targets are forbidden")
    origin = runtime_inventory.get("origin", "")
    parsed = urlparse(origin)
    _require(parsed.scheme in {"http", "https"} and parsed.netloc, "runtime origin is invalid")
    image = artifact_lock.get("image", "")
    _require("@sha256:" in image, "runtime artifact must be digest-pinned")
    authorization = create_analysis_manifest("public-source-runtime", repository_lock, artifact_lock)
    authorization.update(
        {
            "runtime": {
                "origin": origin,
                "addresses": runtime_inventory.get("addresses", []),
                "namespace": runtime_inventory.get("namespace"),
                "artifact": image,
                "same_origin_only": True,
                "synthetic_identities_only": True,
                "scan_mode": scan_mode,
                "allowed_paths": runtime_inventory.get("allowed_paths", []),
                "synthetic_checks": runtime_inventory.get("synthetic_checks", []),
                "max_requests": max_requests,
                "requests_per_second": requests_per_second,
                "max_concurrency": max_concurrency,
                "max_seconds": max_seconds,
            },
            "prohibited_operations": [
                "credential-attack",
                "denial-of-service",
                "destructive-mutation",
                "host-root-shell",
                "off-origin-request",
                "persistence",
                "privileged-runtime",
                "source-write",
                "patch-output",
                "issue-pr-artifact",
                "publication",
            ],
        }
    )
    return validate_analysis_manifest(authorization)


def _source_files(source: Path) -> Iterable[Path]:
    for item in sorted(source.rglob("*")):
        if ".git" in item.relative_to(source).parts:
            continue
        if item.is_file() and not item.is_symlink():
            yield item


def collect_source_evidence(source: Path, source_lock: dict[str, Any]) -> dict[str, Any]:
    state = repository_state(source)
    _require(state["clean"], "source repository must remain clean")
    _require(state["commit"] == source_lock.get("commit"), "source commit does not match lock")
    files: list[dict[str, Any]] = []
    dependency_locks: list[dict[str, Any]] = []
    for item in _source_files(source):
        relative = item.relative_to(source).as_posix()
        record = {
            "path": relative,
            "size": item.stat().st_size,
            "sha256": f"sha256:{sha256_file(item)}",
        }
        files.append(record)
        if item.name in DEPENDENCY_LOCK_NAMES or (
            item.name.startswith("requirements-") and item.suffix == ".txt"
        ):
            dependency_locks.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": state["commit"],
        "file_count": len(files),
        "tree_sha256": f"sha256:{hashlib.sha256(canonical_json(files)).hexdigest()}",
        "files": files,
        "dependency_locks": dependency_locks,
        "repository_state": state,
    }


def validate_adapter(adapter: dict[str, Any]) -> dict[str, Any]:
    _require(adapter.get("schema_version") == SCHEMA_VERSION, "unsupported adapter schema")
    _require(adapter.get("kind") == "source-runtime", "adapter kind must be source-runtime")
    _require(isinstance(adapter.get("name"), str) and adapter["name"], "adapter name is required")
    build = adapter.get("build")
    runtime = adapter.get("runtime")
    _require(isinstance(build, dict), "build contract is required")
    _require(isinstance(runtime, dict), "runtime contract is required")
    _require(build.get("engine") == "docker-buildx-oci", "only docker-buildx-oci is supported")
    _require(build.get("network") == "none", "build network must be none")
    _relative_path(str(build.get("dockerfile", "")), "build.dockerfile")
    context = str(build.get("context", "."))
    if context != ".":
        _relative_path(context, "build.context")
    _require(build.get("platform") == "linux/amd64", "build platform must be linux/amd64")
    _require(isinstance(runtime.get("container_port"), int), "runtime.container_port is required")
    _require(1 <= runtime["container_port"] <= 65535, "runtime.container_port is invalid")
    _require(str(runtime.get("health_path", "")).startswith("/"), "health_path must be absolute")
    _require(runtime.get("run_as_non_root") is True, "runtime must run as non-root")
    _require(runtime.get("read_only_root_filesystem") is True, "runtime root filesystem must be read-only")
    allowed_paths = runtime.get("allowed_paths", [])
    _require(isinstance(allowed_paths, list) and allowed_paths, "runtime.allowed_paths is required")
    for path in allowed_paths:
        _require(isinstance(path, str) and path.startswith("/"), "allowed paths must be absolute")
    checks = runtime.get("synthetic_checks", [])
    _require(isinstance(checks, list), "runtime.synthetic_checks must be a list")
    for check in checks:
        _require(check.get("method") in {"GET", "HEAD"}, "synthetic checks support GET/HEAD only")
        _require(check.get("path") in allowed_paths, "synthetic check path is not allowed")
        _require(isinstance(check.get("expected_status"), int), "expected_status is required")
        authorization = check.get("authorization")
        if authorization is not None:
            _require(
                isinstance(authorization, str) and authorization.startswith("Bearer "),
                "fixture authorization must be a synthetic bearer value",
            )
    return adapter


def adapter_digest(path: Path) -> str:
    return f"sha256:{sha256_file(path)}"


def render_build_command(
    source: Path,
    adapter: dict[str, Any],
    output_archive: Path,
) -> list[str]:
    validate_adapter(adapter)
    build = adapter["build"]
    context = source if build.get("context", ".") == "." else source / build["context"]
    dockerfile = source / build["dockerfile"]
    _require(dockerfile.is_file(), f"Dockerfile does not exist: {dockerfile}")
    _require(context.is_dir(), f"build context does not exist: {context}")
    return [
        "docker",
        "buildx",
        "build",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "--file",
        str(dockerfile),
        "--output",
        f"type=oci,dest={output_archive}",
        str(context),
    ]


def _oci_manifest_digest(archive: Path) -> str:
    with tarfile.open(archive, "r") as stream:
        member = stream.getmember("index.json")
        payload = stream.extractfile(member)
        _require(payload is not None, "OCI archive index.json is unreadable")
        index = json.load(payload)
    manifests = index.get("manifests", [])
    _require(len(manifests) == 1, "OCI archive must contain exactly one platform manifest")
    digest = manifests[0].get("digest", "")
    _require_sha256(digest, "OCI manifest digest")
    return digest


def build_source_runtime(
    source: Path,
    source_lock: dict[str, Any],
    adapter_path: Path,
    output: Path,
    execute: bool,
) -> dict[str, Any]:
    before = repository_state(source)
    _require(before["clean"], "source repository must be clean")
    _require(before["commit"] == source_lock.get("commit"), "source commit does not match lock")
    adapter = validate_adapter(_load_json(adapter_path))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "runtime.oci.tar"
    with tempfile.TemporaryDirectory(prefix="ai-build-tools-source-runtime-") as temporary:
        workspace = Path(temporary) / "source"
        shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(".git"))
        command = render_build_command(workspace, adapter, archive)
        write_json(output / "build-command.json", {"argv": command, "executed": execute})
        if execute:
            subprocess.run(command, check=True)
    manifest_digest = _oci_manifest_digest(archive) if execute else None
    after = verify_repository_unchanged(source, before)
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "BUILT" if execute else "PLANNED",
        "source_commit": before["commit"],
        "source_tree_modified": False,
        "management_credentials_present": False,
        "adapter": adapter["name"],
        "adapter_digest": adapter_digest(adapter_path),
        "build_network": "none",
        "platform": "linux/amd64",
        "oci_archive": str(archive) if execute else None,
        "oci_archive_sha256": f"sha256:{sha256_file(archive)}" if execute else None,
        "manifest_digest": manifest_digest,
        "image": f"oci-archive:{archive}@{manifest_digest}" if execute else None,
        "repository_state_after": after,
    }
    write_json(output / "runtime-artifact-lock.json", result)
    return result


def _dns_label(value: str, field: str) -> str:
    _require(
        len(value) <= 63
        and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", value) is not None,
        f"{field} must be a Kubernetes DNS label",
    )
    return value


def render_kubernetes_runtime(
    artifact_lock: dict[str, Any],
    adapter: dict[str, Any],
    namespace: str,
    name: str,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validate_adapter(adapter)
    _dns_label(namespace, "namespace")
    _dns_label(name, "name")
    image = artifact_lock.get("image", "")
    _require("@sha256:" in image, "published runtime image must be digest-pinned")
    digest = image.rsplit("@", 1)[1]
    _require_sha256(digest, "published runtime image")
    runtime = adapter["runtime"]
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/managed-by": "ai-build-tools",
        "ai-build-tools.ricolin.dev/security-research-run": run_id,
    }
    namespace_manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "ai-build-tools.ricolin.dev/security-research-run": run_id,
            },
        },
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "site",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [
                                {
                                    "name": "http",
                                    "containerPort": runtime["container_port"],
                                    "protocol": "TCP",
                                }
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": runtime["health_path"], "port": "http"}
                            },
                            "livenessProbe": {
                                "httpGet": {"path": runtime["health_path"], "port": "http"}
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "selector": {"app.kubernetes.io/name": name},
            "ports": [{"name": "http", "port": runtime["container_port"], "targetPort": "http"}],
        },
    }
    default_deny = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
        },
    }
    allow_research_ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "allow-research-ingress", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "ai-build-tools.ricolin.dev/security-research-run": run_id
                                }
                            },
                            "podSelector": {
                                "matchExpressions": [
                                    {
                                        "key": "ai-build-tools.ricolin.dev/security-role",
                                        "operator": "In",
                                        "values": ["collector", "proof-runner"],
                                    }
                                ]
                            },
                        }
                    ],
                    "ports": [{"port": runtime["container_port"], "protocol": "TCP"}],
                }
            ],
        },
    }
    origin = f"http://{name}.{namespace}.svc.cluster.local:{runtime['container_port']}"
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "artifact": image,
        "namespace": namespace,
        "name": name,
        "origin": origin,
        "addresses": [f"{name}.{namespace}.svc.cluster.local"],
        "community_operated": False,
        "allowed_paths": runtime["allowed_paths"],
        "synthetic_checks": runtime["synthetic_checks"],
        "run_id": run_id,
    }
    return [namespace_manifest, deployment, service, default_deny, allow_research_ingress], inventory


def write_kubernetes_runtime(
    artifact_lock: dict[str, Any],
    adapter: dict[str, Any],
    namespace: str,
    name: str,
    run_id: str,
    output: Path,
) -> None:
    manifests, inventory = render_kubernetes_runtime(artifact_lock, adapter, namespace, name, run_id)
    output.mkdir(parents=True, exist_ok=True)
    for index, manifest in enumerate(manifests):
        kind = str(manifest["kind"]).lower()
        write_json(output / f"{index:02d}-{kind}.json", manifest)
    write_json(output / "runtime-inventory.json", inventory)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _safe_response_body(response: Any, maximum: int = 1024 * 1024) -> bytes:
    body = response.read(maximum + 1)
    _require(len(body) <= maximum, "response body exceeds evidence limit")
    return body


def collect_site_evidence(authorization: dict[str, Any], output: Path) -> dict[str, Any]:
    validate_analysis_manifest(authorization)
    runtime = authorization.get("runtime", {})
    _require(runtime.get("same_origin_only") is True, "same-origin enforcement is required")
    origin = runtime.get("origin", "")
    origin_parts = urlparse(origin)
    _require(origin_parts.scheme in {"http", "https"} and origin_parts.netloc, "invalid origin")
    checks = runtime.get("synthetic_checks", [])
    _require(isinstance(checks, list) and checks, "synthetic checks are required")
    _require(len(checks) <= runtime.get("max_requests", 0), "checks exceed request budget")
    opener = build_opener(_NoRedirect())
    results: list[dict[str, Any]] = []
    proofs: list[dict[str, Any]] = []
    for check in checks:
        url = urljoin(f"{origin.rstrip('/')}/", check["path"].lstrip("/"))
        parsed = urlparse(url)
        _require(
            (parsed.scheme, parsed.netloc) == (origin_parts.scheme, origin_parts.netloc),
            "off-origin request is forbidden",
        )
        headers = {"User-Agent": "ai-build-tools-security-research/1"}
        if check.get("authorization"):
            headers["Authorization"] = check["authorization"]
        request = Request(url, method=check["method"], headers=headers)
        try:
            response = opener.open(request, timeout=min(runtime.get("max_seconds", 30), 30))
            status = response.status
            response_headers = dict(response.headers.items())
            body = _safe_response_body(response)
        except HTTPError as error:
            status = error.code
            response_headers = dict(error.headers.items())
            body = _safe_response_body(error)
        record = {
            "check_id": check["id"],
            "method": check["method"],
            "path": check["path"],
            "identity": check.get("identity", "anonymous"),
            "expected_status": check["expected_status"],
            "observed_status": status,
            "response_sha256": f"sha256:{hashlib.sha256(body).hexdigest()}",
            "response_headers": {
                key: value
                for key, value in response_headers.items()
                if key.lower() in {"content-type", "content-length", "cache-control", "location"}
            },
        }
        results.append(record)
        if check.get("proof_label") and status != check["expected_status"]:
            payload = json.loads(body)
            canary_digest = payload.get("canary_sha256", "")
            _require(
                len(canary_digest) == 64
                and all(character in "0123456789abcdef" for character in canary_digest),
                "synthetic privilege proof is missing its canary digest",
            )
            proofs.append(
                {
                    "state": check["proof_label"],
                    "check_id": check["id"],
                    "identity": check.get("identity"),
                    "expected_status": check["expected_status"],
                    "observed_status": status,
                    "canary_response_digest": f"sha256:{canary_digest}",
                    "real_host_root_shell": False,
                    "credential_retained": False,
                    "persistence_created": False,
                }
            )
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "origin": origin,
        "request_count": len(results),
        "off_origin_requests": 0,
        "results": results,
        "proofs": proofs,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "site-evidence.json", evidence)
    write_json(output / "proofs.json", proofs)
    return evidence


def validate_finding(finding: dict[str, Any]) -> dict[str, Any]:
    _require(finding.get("proof_state") in PROOF_STATES, "invalid proof_state")
    classification = finding.get("research_classification")
    _require(classification in RESEARCH_CLASSIFICATIONS, "invalid research_classification")
    _require(finding.get("upstream_change_authorized") is False, "upstream changes must be unauthorized")
    _require(finding.get("public_disclosure_authorized") is False, "public disclosure must be unauthorized")
    evidence = finding.get("evidence")
    _require(isinstance(evidence, list), "finding evidence must be a list")
    if classification == "POTENTIALLY_NOVEL":
        novelty = finding.get("novelty_gate", {})
        reproductions = novelty.get("independent_reproductions", [])
        _require(len(reproductions) >= 2, "potential novelty requires two reproductions")
        environments = {record.get("environment_id") for record in reproductions}
        _require(None not in environments and len(environments) >= 2, "reproductions must be independent")
        _require(novelty.get("positive_control") is True, "positive control is required")
        _require(novelty.get("negative_control") is True, "negative control is required")
        _require(novelty.get("affected_bounds"), "affected bounds are required")
        _require(novelty.get("unaffected_bounds"), "unaffected bounds are required")
        _require(novelty.get("source_location"), "source location is required")
        _require(novelty.get("root_cause_evidence"), "root-cause evidence is required")
        search = novelty.get("known_work_search", {})
        _require(search.get("complete") is True, "complete known-work search is required")
    return finding


def render_private_reports(finding: dict[str, Any], output: Path) -> None:
    validate_finding(finding)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "private-finding.json", finding)
    evidence_lines = "\n".join(f"- `{item}`" for item in finding.get("evidence", [])) or "- None"
    title = finding.get("title", "Security research finding")
    report = f"""# {title}

## Classification

- Proof state: `{finding['proof_state']}`
- Research classification: `{finding['research_classification']}`
- Confidence: `{finding.get('confidence', 'UNSPECIFIED')}`

## Observation

{finding.get('observation', 'No observation supplied.')}

## Evidence

{evidence_lines}

## Scope Boundary

This is an analysis-only private report. It does not authorize a source change,
patch, branch, commit, issue, pull request, comment, or public disclosure.
"""
    (output / "private-finding.md").write_text(report)
    recommendation = f"""# Remediation Recommendations

## Likely Ownership

{finding.get('likely_owner', 'UNKNOWN_MULTI_REPOSITORY')}

## Recommended Investigation

{finding.get('remediation_recommendation', 'Collect more evidence before selecting a correction.')}

## Compatibility Risks

{finding.get('compatibility_risks', 'Not yet established.')}

This report intentionally contains no patch or upstream publication artifact.
"""
    (output / "remediation-recommendations.md").write_text(recommendation)
    tests = f"""# Regression-Test Recommendations

{finding.get('regression_test_recommendation', 'Re-run the frozen reproduction and negative control.')}

This is a test recommendation, not an implemented source change.
"""
    (output / "regression-test-recommendations.md").write_text(tests)


def verify_reports_evidence_only(assessment: Path) -> dict[str, Any]:
    _require(assessment.is_dir(), f"assessment directory does not exist: {assessment}")
    prohibited: list[str] = []
    files: list[dict[str, Any]] = []
    for item in sorted(assessment.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(assessment).as_posix()
        if item.name.lower() in PROHIBITED_REPORT_NAMES or item.suffix.lower() in PROHIBITED_REPORT_SUFFIXES:
            prohibited.append(relative)
        files.append(
            {
                "path": relative,
                "size": item.stat().st_size,
                "sha256": f"sha256:{sha256_file(item)}",
            }
        )
    if prohibited:
        raise ContractError(f"prohibited patch/issue/PR artifacts found: {', '.join(prohibited)}")
    required = {
        "private-finding.json",
        "private-finding.md",
        "remediation-recommendations.md",
        "regression-test-recommendations.md",
    }
    observed = {record["path"] for record in files}
    missing = sorted(required - observed)
    _require(not missing, f"missing required reports: {', '.join(missing)}")
    finding = _load_json(assessment / "private-finding.json")
    validate_finding(finding)
    return {
        "schema_version": SCHEMA_VERSION,
        "pass": True,
        "prohibited_artifacts": [],
        "files": files,
        "evidence_index_sha256": f"sha256:{hashlib.sha256(canonical_json(files)).hexdigest()}",
    }


def _command_capabilities(_args: argparse.Namespace) -> None:
    capabilities = [
        "research-source-lock-v1",
        "research-reports-evidence-only-v1",
        "research-novelty-gate-v1",
        "research-buildx-oci-network-none-v1",
        "research-kubernetes-runtime-render-v1",
        "research-synthetic-canary-collector-v1",
    ]
    print("\n".join(capabilities))


def _command_lock_source(args: argparse.Namespace) -> None:
    create_repository_lock(Path(args.source), args.repository, args.requested_ref, Path(args.output))


def _command_snapshot_source(args: argparse.Namespace) -> None:
    write_json(Path(args.output), repository_state(Path(args.source)))


def _command_verify_source(args: argparse.Namespace) -> None:
    before = _load_json(Path(args.before))
    write_json(Path(args.output), verify_repository_unchanged(Path(args.source), before))


def _command_create_manifest(args: argparse.Namespace) -> None:
    source_lock = _load_json(Path(args.source_lock)) if args.source_lock else None
    target_lock = _load_json(Path(args.target_lock)) if args.target_lock else None
    write_json(Path(args.output), create_analysis_manifest(args.selector, source_lock, target_lock))


def _command_validate_manifest(args: argparse.Namespace) -> None:
    validate_analysis_manifest(_load_json(Path(args.manifest)))


def _command_collect_source(args: argparse.Namespace) -> None:
    evidence = collect_source_evidence(Path(args.source), _load_json(Path(args.source_lock)))
    write_json(Path(args.output), evidence)


def _command_validate_adapter(args: argparse.Namespace) -> None:
    adapter_path = Path(args.adapter)
    value = validate_adapter(_load_json(adapter_path))
    write_json(Path(args.output), {"adapter": value, "digest": adapter_digest(adapter_path)})


def _command_build_runtime(args: argparse.Namespace) -> None:
    build_source_runtime(
        Path(args.source),
        _load_json(Path(args.source_lock)),
        Path(args.adapter),
        Path(args.output),
        args.execute,
    )


def _command_create_authorization(args: argparse.Namespace) -> None:
    authorization = create_runtime_authorization(
        _load_json(Path(args.repository_lock)),
        _load_json(Path(args.artifact_lock)),
        _load_json(Path(args.runtime_inventory)),
        args.scan_mode,
        args.max_requests,
        args.requests_per_second,
        args.max_concurrency,
        args.max_seconds,
    )
    write_json(Path(args.output), authorization)


def _command_render_runtime(args: argparse.Namespace) -> None:
    write_kubernetes_runtime(
        _load_json(Path(args.artifact_lock)),
        _load_json(Path(args.adapter)),
        args.namespace,
        args.name,
        args.run_id,
        Path(args.output),
    )


def _command_collect_site(args: argparse.Namespace) -> None:
    collect_site_evidence(_load_json(Path(args.authorization)), Path(args.output))


def _command_render_reports(args: argparse.Namespace) -> None:
    render_private_reports(_load_json(Path(args.finding)), Path(args.output))


def _command_verify_reports(args: argparse.Namespace) -> None:
    report = verify_reports_evidence_only(Path(args.assessment))
    write_json(Path(args.output), report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analysis-only source and runtime security research")
    commands = parser.add_subparsers(dest="command", required=True)

    capabilities = commands.add_parser("capabilities")
    capabilities.set_defaults(handler=_command_capabilities)

    lock_source = commands.add_parser("lock-source")
    lock_source.add_argument("--source", required=True)
    lock_source.add_argument("--repository", required=True)
    lock_source.add_argument("--requested-ref", required=True)
    lock_source.add_argument("--output", required=True)
    lock_source.set_defaults(handler=_command_lock_source)

    snapshot = commands.add_parser("snapshot-source")
    snapshot.add_argument("--source", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.set_defaults(handler=_command_snapshot_source)

    verify = commands.add_parser("verify-source-unchanged")
    verify.add_argument("--source", required=True)
    verify.add_argument("--before", required=True)
    verify.add_argument("--output", required=True)
    verify.set_defaults(handler=_command_verify_source)

    manifest = commands.add_parser("create-analysis-manifest")
    manifest.add_argument("--selector", choices=sorted(RESEARCH_SELECTORS), required=True)
    manifest.add_argument("--source-lock", default="")
    manifest.add_argument("--target-lock", default="")
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(handler=_command_create_manifest)

    validate_manifest = commands.add_parser("validate-analysis-manifest")
    validate_manifest.add_argument("--manifest", required=True)
    validate_manifest.set_defaults(handler=_command_validate_manifest)

    collect = commands.add_parser("collect-source")
    collect.add_argument("--source", required=True)
    collect.add_argument("--source-lock", required=True)
    collect.add_argument("--output", required=True)
    collect.set_defaults(handler=_command_collect_source)

    adapter = commands.add_parser("validate-adapter")
    adapter.add_argument("--adapter", required=True)
    adapter.add_argument("--output", required=True)
    adapter.set_defaults(handler=_command_validate_adapter)

    build = commands.add_parser("build-source-runtime")
    build.add_argument("--source", required=True)
    build.add_argument("--source-lock", required=True)
    build.add_argument("--adapter", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--execute", action="store_true")
    build.set_defaults(handler=_command_build_runtime)

    authorization = commands.add_parser("create-runtime-authorization")
    authorization.add_argument("--repository-lock", required=True)
    authorization.add_argument("--artifact-lock", required=True)
    authorization.add_argument("--runtime-inventory", required=True)
    authorization.add_argument("--scan-mode", choices=("passive", "active-safe-canary"), default="passive")
    authorization.add_argument("--max-requests", type=int, default=2500)
    authorization.add_argument("--requests-per-second", type=int, default=5)
    authorization.add_argument("--max-concurrency", type=int, default=2)
    authorization.add_argument("--max-seconds", type=int, default=1800)
    authorization.add_argument("--output", required=True)
    authorization.set_defaults(handler=_command_create_authorization)

    runtime = commands.add_parser("render-kubernetes-runtime")
    runtime.add_argument("--artifact-lock", required=True)
    runtime.add_argument("--adapter", required=True)
    runtime.add_argument("--namespace", required=True)
    runtime.add_argument("--name", required=True)
    runtime.add_argument("--run-id", required=True)
    runtime.add_argument("--output", required=True)
    runtime.set_defaults(handler=_command_render_runtime)

    site = commands.add_parser("collect-site")
    site.add_argument("--authorization", required=True)
    site.add_argument("--output", required=True)
    site.set_defaults(handler=_command_collect_site)

    reports = commands.add_parser("render-private-reports")
    reports.add_argument("--finding", required=True)
    reports.add_argument("--output", required=True)
    reports.set_defaults(handler=_command_render_reports)

    verify_reports = commands.add_parser("verify-reports-evidence-only")
    verify_reports.add_argument("--assessment", required=True)
    verify_reports.add_argument("--output", required=True)
    verify_reports.set_defaults(handler=_command_verify_reports)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except ContractError as error:
        raise SystemExit(f"contract error: {error}") from error


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_build_tools_k8s.code_review_agent import validate_candidate_fix
from ai_build_tools_k8s.code_review_model import ContractError, load_json, require, require_image
from ai_build_tools_k8s.workflow import write_json


DNS_EGRESS = [
    {"protocol": "UDP", "port": 53},
    {"protocol": "TCP", "port": 53},
]


def dns_label(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    require(bool(normalized) and len(normalized) <= 63, "name is not a valid DNS label")
    return normalized


def validate_source_lock(lock: dict[str, Any]) -> dict[str, Any]:
    repository = str(lock.get("repository", ""))
    parsed = urlparse(repository)
    require(parsed.scheme == "https" and parsed.netloc, "repository must use public HTTPS")
    commit = str(lock.get("commit", ""))
    require(len(commit) == 40 and all(character in "0123456789abcdef" for character in commit), "invalid commit")
    require(isinstance(lock.get("id"), str) and lock["id"], "source lock id is required")
    return lock


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    require(profile.get("schema_version") == "1.0.0", "unsupported profile schema")
    require(isinstance(profile.get("id"), str) and profile["id"], "profile id is required")
    for field in ("fetch_image", "runner_image"):
        require_image(str(profile.get(field, "")), field)
    for field in ("prepare_commands", "test_commands"):
        commands = profile.get(field)
        require(isinstance(commands, list), f"{field} must be a list")
        for command in commands:
            require(isinstance(command, list) and command, f"{field} entries must be argv arrays")
            require(all(isinstance(argument, str) and argument for argument in command), "empty command argument")
    require(profile["test_commands"], "at least one test command is required")
    require(1 <= int(profile.get("timeout_seconds", 0)) <= 3600, "profile timeout is invalid")
    return profile


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def script(commands: list[list[str]]) -> str:
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", "cd /workspace/source"]
    lines.extend(" ".join(shell_quote(argument) for argument in command) for command in commands)
    return "\n".join(lines) + "\n"


def fetch_script(repository: str, commit: str) -> str:
    return "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            "if [ -e /workspace/source ]; then",
            "  test -d /workspace/source",
            "  test -z \"$(find /workspace/source -mindepth 1 -maxdepth 1 -print -quit)\"",
            "  rmdir /workspace/source",
            "fi",
            f"GIT_TERMINAL_PROMPT=0 git clone --filter=blob:none --no-checkout {shell_quote(repository)} /workspace/source",
            f"git -C /workspace/source checkout --detach {shell_quote(commit)}",
            f"test \"$(git -C /workspace/source rev-parse HEAD)\" = {shell_quote(commit)}",
            "test -z \"$(git -C /workspace/source status --porcelain)\"",
        ]
    ) + "\n"


def patch_test_script(commands: list[list[str]], patch_present: bool) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        "cd /workspace/source",
        "mkdir -p /workspace/results",
        "test -z \"$(git status --porcelain)\" || exit 20",
    ]
    if patch_present:
        lines.extend(
            [
                "git apply --check /opt/review/fix.patch || exit 21",
                "git apply /opt/review/fix.patch || exit 22",
                "git diff --check || exit 23",
            ]
        )
    lines.extend(["status=0", ": > /workspace/results/unit-tests.log"])
    for command in commands:
        rendered = " ".join(shell_quote(argument) for argument in command)
        lines.append(f"{rendered} 2>&1 | tee -a /workspace/results/unit-tests.log || status=$?")
        lines.append("test \"${status}\" -eq 0 || break")
    lines.extend(
        [
            "git diff --no-ext-diff --src-prefix=a/ --dst-prefix=b/ > /workspace/results/fix.patch",
            "sha256sum /workspace/results/fix.patch > /workspace/results/fix.patch.sha256",
            "printf 'UNIT_TEST_STATUS=%s\\n' \"${status}\" > /workspace/results/result.env",
            "printf 'SOURCE_COMMIT=%s\\n' \"$(git rev-parse HEAD)\" >> /workspace/results/result.env",
            "exit \"${status}\"",
        ]
    )
    return "\n".join(lines) + "\n"


def pod_security() -> dict[str, Any]:
    return {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def container_security() -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }


def job(
    namespace: str,
    name: str,
    image: str,
    command: list[str],
    pvc: str,
    network_stage: str,
    timeout_seconds: int,
    script_config_map: str = "",
) -> dict[str, Any]:
    mounts = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "home", "mountPath": "/home/sandbox"},
    ]
    volumes: list[dict[str, Any]] = [
        {"name": "workspace", "persistentVolumeClaim": {"claimName": pvc}},
        {"name": "tmp", "emptyDir": {}},
        {"name": "home", "emptyDir": {}},
    ]
    if script_config_map:
        mounts.append({"name": "script", "mountPath": "/opt/review", "readOnly": True})
        volumes.append({"name": "script", "configMap": {"name": script_config_map, "defaultMode": 365}})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_seconds,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "code-review-sandbox",
                        "ai-k8s-tools.ricolin.dev/network-stage": network_stage,
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": pod_security(),
                    "containers": [
                        {
                            "name": "runner",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": command,
                            "env": [
                                {"name": "HOME", "value": "/home/sandbox"},
                                {"name": "XDG_CACHE_HOME", "value": "/workspace/.cache"},
                            ],
                            "resources": {
                                "requests": {"cpu": "1", "memory": "1Gi"},
                                "limits": {"cpu": "8", "memory": "16Gi"},
                            },
                            "securityContext": container_security(),
                            "volumeMounts": mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def render_bundle(
    lock: dict[str, Any],
    profile: dict[str, Any],
    namespace: str,
    pvc: str,
    storage_class: str,
    candidate_fix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_source_lock(lock)
    validate_profile(profile)
    namespace = dns_label(namespace)
    pvc = dns_label(pvc)
    profile_name = dns_label(profile["id"])
    fetch_name = f"review-fetch-{profile_name}"
    prepare_name = f"review-prepare-{profile_name}"
    test_name = f"review-test-{profile_name}"
    prepare_script = script(profile["prepare_commands"] or [["true"]])
    if candidate_fix is None:
        candidate_fix = {
            "status": "NOT_NEEDED",
            "patch_id": None,
            "unified_diff": "",
            "rationale": "Review-only sandbox run",
            "expected_tests": [],
        }
    validate_candidate_fix(candidate_fix)
    patch_present = candidate_fix["status"] == "PROPOSED"
    test_script = patch_test_script(profile["test_commands"], patch_present)
    pvc_spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "20Gi"}},
    }
    if storage_class:
        pvc_spec["storageClassName"] = storage_class
    return {
        "namespace.json": {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/warn": "restricted",
                },
            },
        },
        "pvc.json": {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc, "namespace": namespace},
            "spec": pvc_spec,
        },
        "default-deny.json": {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny", "namespace": namespace},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        },
        "fetch-egress.json": {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "fetch-and-prepare-egress", "namespace": namespace},
            "spec": {
                "podSelector": {
                    "matchExpressions": [
                        {
                            "key": "ai-k8s-tools.ricolin.dev/network-stage",
                            "operator": "In",
                            "values": ["fetch", "prepare"],
                        }
                    ]
                },
                "policyTypes": ["Egress"],
                "egress": [
                    {"ports": DNS_EGRESS},
                    {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"protocol": "TCP", "port": 443}]},
                ],
            },
        },
        "prepare-script.json": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{profile_name}-prepare", "namespace": namespace},
            "data": {"run.sh": prepare_script},
        },
        "fetch-script.json": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{profile_name}-fetch", "namespace": namespace},
            "data": {"run.sh": fetch_script(lock["repository"], lock["commit"])},
        },
        "test-script.json": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"{profile_name}-test", "namespace": namespace},
            "data": {
                "run.sh": test_script,
                "fix.patch": candidate_fix["unified_diff"],
            },
        },
        "fetch-job.json": job(
            namespace,
            fetch_name,
            profile["fetch_image"],
            ["/bin/sh", "/opt/review/run.sh"],
            pvc,
            "fetch",
            int(profile["timeout_seconds"]),
            f"{profile_name}-fetch",
        ),
        "prepare-job.json": job(
            namespace,
            prepare_name,
            profile["runner_image"],
            ["/opt/review/run.sh"],
            pvc,
            "prepare",
            int(profile["timeout_seconds"]),
            f"{profile_name}-prepare",
        ),
        "test-job.json": job(
            namespace,
            test_name,
            profile["runner_image"],
            ["/opt/review/run.sh"],
            pvc,
            "test",
            int(profile["timeout_seconds"]),
            f"{profile_name}-test",
        ),
        "result-contract.json": {
            "schema_version": "1.0.0",
            "repository_lock_id": lock["id"],
            "repository": lock["repository"],
            "commit": lock["commit"],
            "profile_id": profile["id"],
            "patch_id": candidate_fix["patch_id"],
            "outputs": [
                "/workspace/results/fix.patch",
                "/workspace/results/fix.patch.sha256",
                "/workspace/results/unit-tests.log",
                "/workspace/results/result.env",
            ],
            "note": "A patch is accepted only when the test Job completes and SOURCE_COMMIT matches this lock.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a profile-driven Kubernetes code-review sandbox")
    parser.add_argument("--source-lock", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pvc", required=True)
    parser.add_argument("--storage-class", default="")
    parser.add_argument("--candidate-response", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        candidate_fix = None
        if args.candidate_response:
            response = load_json(Path(args.candidate_response))
            require(isinstance(response.get("candidate_fix"), dict), "candidate response is missing candidate_fix")
            candidate_fix = response["candidate_fix"]
        bundle = render_bundle(
            load_json(Path(args.source_lock)),
            load_json(Path(args.profile)),
            args.namespace,
            args.pvc,
            args.storage_class,
            candidate_fix,
        )
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        for name, value in bundle.items():
            write_json(output / name, value)
    except ContractError as error:
        raise SystemExit(f"contract error: {error}") from error


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Allow generated controller workloads on an explicitly selected control plane."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


CONTROL_PLANE_TOLERATIONS = (
    {
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
        "effect": "NoSchedule",
    },
    {
        "key": "node-role.kubernetes.io/master",
        "operator": "Exists",
        "effect": "NoSchedule",
    },
)


def pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    kind = document.get("kind")
    spec = document.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (
            spec.get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec")
        )
    if kind in {
        "DaemonSet",
        "Deployment",
        "Job",
        "ReplicaSet",
        "StatefulSet",
    }:
        return spec.get("template", {}).get("spec")
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output_documents = []
    changed = 0
    for source_document in args.input.read_text().split("\n---\n"):
        if not source_document.strip():
            continue
        document = yaml.safe_load(source_document)
        if not isinstance(document, dict):
            output_documents.append(source_document.rstrip())
            continue
        spec = pod_spec(document)
        if not isinstance(spec, dict):
            output_documents.append(source_document.rstrip())
            continue
        tolerations = spec.setdefault("tolerations", [])
        if not isinstance(tolerations, list):
            raise SystemExit(
                "workload tolerations must be a list: "
                f"{document.get('kind')}/{document.get('metadata', {}).get('name')}"
            )
        for required in CONTROL_PLANE_TOLERATIONS:
            if required not in tolerations:
                tolerations.append(dict(required))
        changed += 1
        output_documents.append(
            yaml.safe_dump(document, sort_keys=False).rstrip()
        )

    if changed == 0:
        raise SystemExit("no workload pod specs found")
    args.output.write_text("\n---\n".join(output_documents) + "\n")


if __name__ == "__main__":
    main()

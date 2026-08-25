#!/usr/bin/env python3
"""Apply cluster scheduling defaults to every Argo Workflow template."""

from __future__ import annotations

import argparse
from pathlib import Path

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
        identity = (
            document.get("kind"),
            document.get("metadata", {}).get("namespace"),
            document.get("metadata", {}).get("name"),
        ) if isinstance(document, dict) else (None, None, None)
        if identity != (
            "ConfigMap",
            "kubeflow",
            "workflow-controller-configmap",
        ):
            output_documents.append(source_document.rstrip())
            continue

        data = document.get("data", {})
        if not isinstance(data, dict):
            raise SystemExit("workflow controller ConfigMap data must be a map")
        if "workflowDefaults" in data:
            raise SystemExit(
                "upstream now defines workflowDefaults; merge it deliberately"
            )
        marker = "\nkind: ConfigMap\nmetadata:"
        if source_document.count(marker) != 1:
            raise SystemExit(
                "workflow controller ConfigMap kind/metadata marker is not unique"
            )
        rendered_defaults = (
            "\n  workflowDefaults: |\n"
            "    spec:\n"
            "      templateDefaults:\n"
            "        tolerations:\n"
        )
        for toleration in CONTROL_PLANE_TOLERATIONS:
            rendered_defaults += (
                f"        - key: {toleration['key']}\n"
                f"          operator: {toleration['operator']}\n"
                f"          effect: {toleration['effect']}\n"
            )
        output_documents.append(
            source_document.replace(
                marker,
                rendered_defaults.rstrip("\n") + marker,
            ).rstrip()
        )
        changed += 1

    if changed != 1:
        raise SystemExit(
            "expected one kubeflow/workflow-controller-configmap, "
            f"changed {changed}"
        )
    args.output.write_text("\n---\n".join(output_documents) + "\n")


if __name__ == "__main__":
    main()

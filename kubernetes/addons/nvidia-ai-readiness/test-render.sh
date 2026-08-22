#!/usr/bin/env bash
set -euo pipefail

chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
temporary=$(mktemp -d /tmp/nvidia-ai-readiness-render.XXXXXX)
trap 'rm -rf "${temporary}"' EXIT

runtime_arg_line=$(grep -n '^ARG CUDA_RUNTIME_IMAGE$' \
  "${chart_dir}/images/cuda-smoke/Dockerfile" | cut -d: -f1)
first_from_line=$(grep -n '^FROM ' \
  "${chart_dir}/images/cuda-smoke/Dockerfile" | head -1 | cut -d: -f1)
[[ ${runtime_arg_line} -lt ${first_from_line} ]]

python3 - "${chart_dir}/artifacts.lock.yaml" <<'PY'
import re
import sys

import yaml

lock = yaml.safe_load(open(sys.argv[1]))
assert lock["sourceCommit"] == "14d9dd33f27d788764f2e9d985cff92eff87cb1c"
assert lock["workflowRun"].endswith("/32593176476")
for artifact in lock["artifacts"].values():
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])
PY

printf '%s  %s\n' \
  6d1b282d74288be206c66dfe49073b7c85c209e92826c27f6507429055e2e102 \
  "${chart_dir}/charts/gpu-operator-v26.7.0.tgz" | sha256sum --check >/dev/null
helm lint "${chart_dir}" >/dev/null
helm template inactive "${chart_dir}" --namespace gpu-operator \
  >"${temporary}/inactive.yaml"
if grep -q '^kind:' "${temporary}/inactive.yaml"; then
  echo "disabled chart rendered Kubernetes resources" >&2
  exit 1
fi

if helm template mutable "${chart_dir}" --namespace gpu-operator \
  -f "${chart_dir}/profiles/managed-driver-values.yaml" \
  --set readiness.orchestratorImage=example.invalid/readiness:latest \
  --set readiness.cudaSmokeImage=example.invalid/cuda:latest \
  >"${temporary}/mutable.yaml" 2>"${temporary}/mutable.err"; then
  echo "mutable readiness images were accepted" >&2
  exit 1
fi
grep -Fq 'must be pinned by sha256 digest' "${temporary}/mutable.err"

orchestrator=registry.example/readiness@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cuda=registry.example/cuda@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
for kube_version in 1.32.10 1.34.8; do
  helm template active "${chart_dir}" --namespace gpu-operator \
    --kube-version "${kube_version}" \
    -f "${chart_dir}/profiles/managed-driver-values.yaml" \
    --set "readiness.orchestratorImage=${orchestrator}" \
    --set "readiness.cudaSmokeImage=${cuda}" \
    --set "readiness.nodeSelector.accelerator=nvidia-h200" \
    --set "readiness.tolerations[0].key=node-role.kubernetes.io/control-plane" \
    --set "readiness.tolerations[0].operator=Exists" \
    --set "readiness.tolerations[0].effect=NoSchedule" \
    --set "readiness.tolerations[1].key=node-role.kubernetes.io/master" \
    --set "readiness.tolerations[1].operator=Exists" \
    --set "readiness.tolerations[1].effect=NoSchedule" \
    --set "readiness.imagePullSecrets[0].name=readiness-registry" \
    >"${temporary}/active-${kube_version}.yaml"
done
active_render="${temporary}/active-1.34.8.yaml"

grep -Fq "image: \"${orchestrator}\"" "${active_render}"
grep -Fq 'operatorChart: "gpu-operator-v26.7.0"' "${active_render}"
grep -Fq 'autoUpgrade: false' "${active_render}"
grep -Fq 'kernelModuleType: auto' "${active_render}"
grep -Fq 'cdi:' "${active_render}"
if grep -Fq 'nriPluginEnabled:' "${active_render}"; then
  echo "NRI was enabled in the managed-driver profile" >&2
  exit 1
fi
grep -Fq 'migManager:' "${active_render}"
grep -Fq 'enabled: false' "${active_render}"

python3 - "${active_render}" <<'PY'
import sys
import json
import yaml

documents = [doc for doc in yaml.safe_load_all(open(sys.argv[1])) if doc]
job = next(
    doc for doc in documents
    if doc.get("kind") == "Job"
    and doc.get("metadata", {}).get("name") == "active-nvidia-ai-readiness"
)
container = job["spec"]["template"]["spec"]["containers"][0]
assert container["securityContext"]["readOnlyRootFilesystem"] is True
assert job["spec"]["backoffLimit"] == 0
assert job["spec"]["activeDeadlineSeconds"] == 3600
environment = {
    item["name"]: item for item in container["env"]
}
assert environment["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
assert environment["CUDA_SMOKE_IMAGE_PULL_POLICY"]["value"] == "IfNotPresent"
assert json.loads(environment["SMOKE_NODE_SELECTOR_JSON"]["value"]) == {
    "accelerator": "nvidia-h200"
}
smoke_tolerations = {
    item["key"]
    for item in json.loads(environment["SMOKE_TOLERATIONS_JSON"]["value"])
}
assert {
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",
} <= smoke_tolerations
assert json.loads(environment["SMOKE_IMAGE_PULL_SECRETS_JSON"]["value"]) == [
    {"name": "readiness-registry"}
]

cluster_role = next(
    doc for doc in documents
    if doc.get("kind") == "ClusterRole"
    and doc.get("metadata", {}).get("name") == "active-nvidia-ai-readiness"
)
assert all(set(rule["verbs"]) <= {"get", "list"} for rule in cluster_role["rules"])

role = next(
    doc for doc in documents
    if doc.get("kind") == "Role"
    and doc.get("metadata", {}).get("name") == "active-nvidia-ai-readiness"
)
assert any("jobs" in rule["resources"] and "create" in rule["verbs"] for rule in role["rules"])
daemonset_rule = next(
    rule for rule in role["rules"] if "daemonsets" in rule["resources"]
)
assert set(daemonset_rule["verbs"]) == {"get", "list", "watch"}

cluster_policy = next(
    doc for doc in documents
    if doc.get("kind") == "ClusterPolicy"
    and doc.get("metadata", {}).get("name") == "cluster-policy"
)
operand_tolerations = {
    item["key"] for item in cluster_policy["spec"]["daemonsets"]["tolerations"]
}
assert {
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",
    "nvidia.com/gpu",
} <= operand_tolerations

nfd_gc = next(
    doc for doc in documents
    if doc.get("kind") == "Deployment"
    and doc.get("metadata", {}).get("name")
    == "active-node-feature-discovery-gc"
)
nfd_gc_tolerations = {
    item["key"] for item in nfd_gc["spec"]["template"]["spec"]["tolerations"]
}
assert {
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",
} <= nfd_gc_tolerations
PY

grep -Fq 'nodeSelector: ${SMOKE_NODE_SELECTOR_JSON}' \
  "${chart_dir}/images/orchestrator/orchestrate.sh"
grep -Fq 'tolerations: ${SMOKE_TOLERATIONS_JSON}' \
  "${chart_dir}/images/orchestrator/orchestrate.sh"
grep -Fq 'imagePullSecrets: ${SMOKE_IMAGE_PULL_SECRETS_JSON}' \
  "${chart_dir}/images/orchestrator/orchestrate.sh"
grep -Fq 'kubectl -n "${TARGET_NAMESPACE}" replace' \
  "${chart_dir}/images/orchestrator/orchestrate.sh"
if grep -Fq -- '--dry-run=client -o yaml | kubectl apply' \
  "${chart_dir}/images/orchestrator/orchestrate.sh"; then
  echo "evidence payload was copied into a kubectl last-applied annotation" >&2
  exit 1
fi
grep -Fq 'uuid_string(properties.uuid)' \
  "${chart_dir}/images/cuda-smoke/cuda-smoke.cu"

echo "PASS: NVIDIA add-on render contract"

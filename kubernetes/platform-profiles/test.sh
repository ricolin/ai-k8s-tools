#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
for chart in ai-foundation ai-platform-core ai-scheduling-kueue \
  ai-training-h200 ai-serving-h200 ai-workflow-bootstrap; do
  chart_dir=${root}/kubernetes/addons/${chart}
  helm lint "${chart_dir}" >/dev/null
  if [[ ${chart} != ai-workflow-bootstrap ]]; then
    test -n "$(find "${chart_dir}/crds" -name 'resources-*.yaml' -print -quit)" || {
      echo "missing CRD pre-install phase for ${chart}" >&2
      exit 1
    }
  fi
  output=$(mktemp "/tmp/${chart}.render.XXXXXX")
  helm template "${chart}" "${chart_dir}" --include-crds >"${output}"
  grep -q '^kind:' "${output}"
  if helm template "${chart}" "${chart_dir}" \
    | grep -Fq 'kind: CustomResourceDefinition'; then
    echo "CRD leaked into normal template phase for ${chart}" >&2
    exit 1
  fi
  python3 - "${chart}" "${output}" <<'PY'
import sys
import yaml

chart, path = sys.argv[1:]
images = []


def walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "image" and isinstance(item, str) and item:
                images.append(item)
            walk(item)
    elif isinstance(value, list):
        for item in value:
            walk(item)


with open(path) as stream:
    for document in yaml.safe_load_all(stream):
        walk(document)
mutable = [image for image in images if "@sha256:" not in image]
if mutable:
    raise SystemExit(f"mutable images rendered by {chart}: {sorted(set(mutable))}")
PY
done

helm template ai-platform-core "${root}/kubernetes/addons/ai-platform-core" \
  | grep -A4 -F '/etc/envoy/envoy-config.yaml' \
  | grep -F -- '--concurrency' >/dev/null
core_output=$(mktemp /tmp/ai-platform-core.namespace.XXXXXX)
helm template ai-platform-core "${root}/kubernetes/addons/ai-platform-core" \
  >"${core_output}"
python3 - "${core_output}" <<'PY'
import sys
import yaml

for document in yaml.safe_load_all(open(sys.argv[1])):
    if (
        isinstance(document, dict)
        and document.get("kind") == "Namespace"
        and document.get("metadata", {}).get("name") == "kubeflow"
    ):
        raise SystemExit("ai-platform-core must not own Namespace/kubeflow")
PY
python3 - "${core_output}" <<'PY'
import sys
import yaml

required = {
    (
        "node-role.kubernetes.io/control-plane",
        "Exists",
        "NoSchedule",
    ),
    ("node-role.kubernetes.io/master", "Exists", "NoSchedule"),
}
matched = 0
for document in yaml.safe_load_all(open(sys.argv[1])):
    if not isinstance(document, dict):
        continue
    identity = (
        document.get("kind"),
        document.get("metadata", {}).get("namespace"),
        document.get("metadata", {}).get("name"),
    )
    if identity != (
        "ConfigMap",
        "kubeflow",
        "workflow-controller-configmap",
    ):
        continue
    matched += 1
    defaults = yaml.safe_load(
        document.get("data", {}).get("workflowDefaults", "")
    )
    tolerations = (
        defaults.get("spec", {})
        .get("templateDefaults", {})
        .get("tolerations", [])
    )
    actual = {
        (item.get("key"), item.get("operator"), item.get("effect"))
        for item in tolerations
    }
    missing = required - actual
    if missing:
        raise SystemExit(
            "workflow controller defaults miss control-plane tolerations: "
            f"{missing}"
        )
if matched != 1:
    raise SystemExit(
        "expected one kubeflow/workflow-controller-configmap, "
        f"found {matched}"
    )
PY
foundation_output=$(mktemp /tmp/ai-foundation.issuer.XXXXXX)
helm template ai-foundation "${root}/kubernetes/addons/ai-foundation" \
  >"${foundation_output}"
python3 - "${foundation_output}" "${core_output}" <<'PY'
import sys
import yaml

foundation_path, core_path = sys.argv[1:]


def identities(path):
    return {
        (document.get("kind"), document.get("metadata", {}).get("name"))
        for document in yaml.safe_load_all(open(path))
        if isinstance(document, dict)
    }


issuer = ("ClusterIssuer", "kubeflow-self-signing-issuer")
if issuer in identities(foundation_path):
    raise SystemExit("ai-foundation must not apply an issuer before its webhook")
if issuer not in identities(core_path):
    raise SystemExit("ai-platform-core must own the cert-manager-dependent issuer")
PY
python3 - "${root}" <<'PY'
import pathlib
import sys
import yaml

root = pathlib.Path(sys.argv[1])
required = {
    (
        "node-role.kubernetes.io/control-plane",
        "Exists",
        "NoSchedule",
    ),
    ("node-role.kubernetes.io/master", "Exists", "NoSchedule"),
}


def pod_spec(document):
    kind = document.get("kind")
    spec = document.get("spec", {})
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


for chart in (
    "ai-foundation",
    "ai-platform-core",
    "ai-scheduling-kueue",
    "ai-training-h200",
    "ai-serving-h200",
):
    manifests = root / "kubernetes" / "addons" / chart / "files"
    workloads = 0
    for path in sorted(manifests.glob("resources-*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not isinstance(document, dict):
                continue
            spec = pod_spec(document)
            if not isinstance(spec, dict):
                continue
            workloads += 1
            actual = {
                (item.get("key"), item.get("operator"), item.get("effect"))
                for item in spec.get("tolerations", [])
            }
            missing = required - actual
            if missing:
                identity = (
                    document.get("kind"),
                    document.get("metadata", {}).get("name"),
                )
                raise SystemExit(
                    f"{chart} workload {identity} misses tolerations: {missing}"
                )
    if not workloads:
        raise SystemExit(f"{chart} contains no workload resources")
PY
helm template ai-scheduling-kueue "${root}/kubernetes/addons/ai-scheduling-kueue" \
  | grep -F 'trainer.kubeflow.org/trainjob' >/dev/null
training_output=$(mktemp /tmp/ai-training-h200.runtime.XXXXXX)
helm template ai-training-h200 \
  "${root}/kubernetes/addons/ai-training-h200" >"${training_output}"
if grep -Fq 'kind: ClusterTrainingRuntime' "${training_output}"; then
  echo 'training controller chart must not create webhook-validated runtimes' >&2
  exit 1
fi
workflow_output=$(mktemp /tmp/ai-workflow-bootstrap.runtime.XXXXXX)
helm template ai-workflow-bootstrap \
  "${root}/kubernetes/addons/ai-workflow-bootstrap" >"${workflow_output}"
grep -F 'kind: ClusterTrainingRuntime' "${workflow_output}" >/dev/null
grep -F 'name: torch-distributed' "${workflow_output}" >/dev/null
grep -F 'name: h200-ai' "${workflow_output}" >/dev/null
grep -F 'namespace: kubeflow' "${workflow_output}" >/dev/null
grep -F 'nvidia.com/gpu.product: NVIDIA-H200' "${workflow_output}" >/dev/null
if [[ $(grep -Fc 'apiVersion: kueue.x-k8s.io/v1beta2' "${workflow_output}") -ne 3 ]]; then
  echo 'workflow profile must use Kueue v1beta2 for all Kueue resources' >&2
  exit 1
fi
grep -F 'nominalQuota: "8"' "${workflow_output}" >/dev/null
grep -F 'nominalQuota: "64"' "${workflow_output}" >/dev/null
grep -F 'nominalQuota: "512Gi"' "${workflow_output}" >/dev/null
if [[ $(grep -Fc '        - name: h200' "${workflow_output}") -ne 1 ]]; then
  echo 'workflow ClusterQueue must declare the H200 flavor exactly once' >&2
  exit 1
fi
if grep -Fq '  cohort:' "${workflow_output}"; then
  echo 'workflow profile must not emit the removed Kueue v1beta1 cohort field' >&2
  exit 1
fi
grep -F 'resources: ["workflowtaskresults"]' "${workflow_output}" >/dev/null
if grep -Fq 'ai-build-tools.ricolin.dev/accelerator' "${workflow_output}"; then
  echo 'workflow profile must use the GPU Operator product label' >&2
  exit 1
fi

profile_verifier=${root}/kubernetes/platform-profiles/verify.sh
bash -n "${profile_verifier}"
grep -F 'namespace=${WORKLOAD_NAMESPACE:-kubeflow}' "${profile_verifier}" >/dev/null
grep -F 'require_deployment kubeflow' "${profile_verifier}" >/dev/null
grep -F 'require_deployment kueue-system' "${profile_verifier}" >/dev/null
grep -F 'require_deployment kubeflow-system' "${profile_verifier}" >/dev/null
grep -F 'app.kubernetes.io/name=nvidia-ai-readiness' \
  "${profile_verifier}" >/dev/null
grep -F 'nvidia-ai-readiness-evidence' "${profile_verifier}" >/dev/null
if grep -Fq 'experiments.kubeflow.org' "${profile_verifier}"; then
  echo 'profile verifier must not require the excluded Katib profile' >&2
  exit 1
fi

echo 'PASS: composable AI platform profile contracts'

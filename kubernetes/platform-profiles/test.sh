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
helm template ai-scheduling-kueue "${root}/kubernetes/addons/ai-scheduling-kueue" \
  | grep -F 'trainer.kubeflow.org/trainjob' >/dev/null
helm template ai-training-h200 "${root}/kubernetes/addons/ai-training-h200" \
  | grep -F 'kind: ClusterTrainingRuntime' >/dev/null
helm template ai-workflow-bootstrap "${root}/kubernetes/addons/ai-workflow-bootstrap" \
  | grep -F 'name: h200-ai' >/dev/null

echo 'PASS: composable AI platform profile contracts'

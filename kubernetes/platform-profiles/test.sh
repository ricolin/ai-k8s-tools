#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
for chart in ai-foundation ai-platform-core ai-scheduling-kueue \
  ai-training-h200 ai-serving-h200 ai-workflow-bootstrap; do
  chart_dir=${root}/kubernetes/addons/${chart}
  helm lint "${chart_dir}" >/dev/null
  if [[ ${chart} != ai-workflow-bootstrap ]]; then
    test -s "${chart_dir}/crds/resources.yaml" || {
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
  | grep -Fq -- '--concurrency'
helm template ai-scheduling-kueue "${root}/kubernetes/addons/ai-scheduling-kueue" \
  | grep -Fq 'trainer.kubeflow.org/trainjob'
helm template ai-training-h200 "${root}/kubernetes/addons/ai-training-h200" \
  | grep -Fq 'kind: ClusterTrainingRuntime'
helm template ai-workflow-bootstrap "${root}/kubernetes/addons/ai-workflow-bootstrap" \
  | grep -Fq 'name: h200-ai'

echo 'PASS: composable AI platform profile contracts'

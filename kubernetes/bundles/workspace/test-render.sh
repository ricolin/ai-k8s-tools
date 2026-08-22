#!/usr/bin/env bash
set -euo pipefail
chart_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
temporary=$(mktemp -d /tmp/ai-workspace-render.XXXXXX)
trap 'rm -rf "${temporary}"' EXIT

helm lint "${chart_dir}" >/dev/null
helm template inactive "${chart_dir}" >"${temporary}/inactive.yaml"
! grep -q '^kind:' "${temporary}/inactive.yaml"
helm template static "${chart_dir}" -f "${chart_dir}/profiles/static-local.example.yaml" \
  >"${temporary}/static.yaml"
for kind in Namespace StorageClass PersistentVolume PersistentVolumeClaim ResourceQuota Job; do
  grep -Fq "kind: ${kind}" "${temporary}/static.yaml"
done
grep -Fq 'helm.sh/resource-policy: keep' "${temporary}/static.yaml"
grep -Fq 'persistentVolumeReclaimPolicy: Retain' "${temporary}/static.yaml"
grep -Fq 'automountServiceAccountToken: false' "${temporary}/static.yaml"
grep -Fq 'runAsNonRoot: true' "${temporary}/static.yaml"
grep -Fq 'runAsUser: 65532' "${temporary}/static.yaml"
grep -Fq 'type: RuntimeDefault' "${temporary}/static.yaml"
grep -Fq 'readOnlyRootFilesystem: true' "${temporary}/static.yaml"
grep -Fq 'mktemp /workspace/.ai-model-workspace-validation.XXXXXX' \
  "${temporary}/static.yaml"
echo "PASS: workspace bundle render contract"

#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the workload cluster}"
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
kubectl_bin=${KUBECTL_BIN:-kubectl}
command -v "${kubectl_bin}" >/dev/null 2>&1 || [[ -x ${kubectl_bin} ]]

KUBECTL_BIN="${kubectl_bin}" "${root_dir}/kubernetes/platform/verify.sh"
for crd in \
  experiments.kubeflow.org \
  trials.kubeflow.org \
  inferenceservices.serving.kserve.io \
  servingruntimes.serving.kserve.io; do
  "${kubectl_bin}" wait --for=condition=Established "crd/${crd}" --timeout=60s
done
"${kubectl_bin}" -n ai-workflows get serviceaccount/ai-build-tools-serving
echo "PASS: pinned Kubeflow, Katib, Model Registry, and KServe APIs are ready"

#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
source "${root_dir}/kubernetes/bundles/kubeflow-kserve/source-lock.env"
source "${root_dir}/kubernetes/versions.env"

for name in KUBEFLOW_DISTRIBUTION_REF KUBEFLOW_DISTRIBUTION_COMMIT \
  KUBEFLOW_PIPELINES_VERSION KUBEFLOW_HUB_VERSION KSERVE_VERSION; do
  bundle_value=$(bash -c "source '${root_dir}/kubernetes/bundles/kubeflow-kserve/source-lock.env'; printf '%s' \"\${${name}}\"")
  [[ ${!name} == "${bundle_value}" ]] || {
    echo "source lock mismatch for ${name}" >&2
    exit 1
  }
done

: "${KUBECONFIG:?set KUBECONFIG to the workload cluster}"
evidence_dir=${EVIDENCE_DIR:-${root_dir}/evidence/kubeflow-kserve}
mkdir -p "${evidence_dir}"
cat >"${evidence_dir}/bundle.env" <<EOF
KUBEFLOW_KSERVE_BUNDLE_VERSION=${KUBEFLOW_KSERVE_BUNDLE_VERSION}
KUBEFLOW_DISTRIBUTION_REF=${KUBEFLOW_DISTRIBUTION_REF}
KUBEFLOW_DISTRIBUTION_COMMIT=${KUBEFLOW_DISTRIBUTION_COMMIT}
KUBEFLOW_PIPELINES_VERSION=${KUBEFLOW_PIPELINES_VERSION}
KUBEFLOW_HUB_VERSION=${KUBEFLOW_HUB_VERSION}
KSERVE_VERSION=${KSERVE_VERSION}
EOF

EVIDENCE_DIR="${evidence_dir}/preflight" \
  "${root_dir}/kubernetes/platform/preflight.sh"
EVIDENCE_DIR="${evidence_dir}/install" \
  "${root_dir}/kubernetes/platform/install.sh"
KUBECTL_BIN=${KUBECTL_BIN:-kubectl} \
  "${root_dir}/kubernetes/bundles/kubeflow-kserve/verify.sh" \
  | tee "${evidence_dir}/bundle-verification.txt"
echo "PASS: Kubeflow/KServe bundle ${KUBEFLOW_KSERVE_BUNDLE_VERSION}"

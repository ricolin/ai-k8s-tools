#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the workload cluster}"
: "${CONFIRM_KUBEFLOW_KSERVE_UNINSTALL:?set to true after reviewing retained data}"
[[ ${CONFIRM_KUBEFLOW_KSERVE_UNINSTALL} == true ]] || {
  echo "CONFIRM_KUBEFLOW_KSERVE_UNINSTALL must be true" >&2
  exit 2
}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
source "${root_dir}/kubernetes/versions.env"
source_root=${SOURCE_ROOT:-/opt/ai-build-tools-sources}
tool_root=${TOOL_ROOT:-/opt/ai-build-tools-bin}
kubectl=${KUBECTL_BIN:-${tool_root}/kubectl}
kustomize=${KUSTOMIZE_BIN:-${tool_root}/kustomize}
source_dir=${source_root}/kubeflow-community-${KUBEFLOW_DISTRIBUTION_COMMIT}
[[ -x ${kubectl} && -x ${kustomize} && -d ${source_dir}/.git ]]
[[ $(git -C "${source_dir}" rev-parse HEAD) == "${KUBEFLOW_DISTRIBUTION_COMMIT}" ]]

pvc_count=$(${kubectl} get pvc -A -o name | wc -l)
if [[ ${pvc_count} -gt 0 && ${ALLOW_KUBEFLOW_DATA_DELETE:-false} != true ]]; then
  echo "refusing uninstall with ${pvc_count} PVCs; export evidence and set ALLOW_KUBEFLOW_DATA_DELETE=true" >&2
  exit 1
fi

evidence_dir=${EVIDENCE_DIR:-${root_dir}/evidence/kubeflow-kserve-uninstall}
mkdir -p "${evidence_dir}"
${kubectl} get all,pvc -A -o yaml >"${evidence_dir}/resources-before.yaml"
cd "${source_dir}"
${kustomize} build applications/kserve/kserve | ${kubectl} delete --ignore-not-found -f -
${kustomize} build applications/katib/upstream/installs/katib-cert-manager | ${kubectl} delete --ignore-not-found -f -
${kustomize} build applications/hub/upstream/overlays/db | ${kubectl} -n kubeflow delete --ignore-not-found -f -
${kustomize} build applications/pipeline/upstream/env/platform-agnostic | ${kubectl} delete --ignore-not-found -f -
${kustomize} build applications/pipeline/upstream/cluster-scoped-resources | ${kubectl} delete --ignore-not-found -f -
${kustomize} build common/cert-manager/overlays/kubeflow | ${kubectl} delete --ignore-not-found -f -
${kustomize} build common/cert-manager/base | ${kubectl} delete --ignore-not-found -f -
${kubectl} delete -f "${root_dir}/kubernetes/manifests/workflow-integration.yaml" --ignore-not-found
echo "PASS: Kubeflow/KServe bundle removed after explicit data-deletion approval"

#!/usr/bin/env bash
set -euo pipefail

: "${KUBECONFIG:?set KUBECONFIG to the target Kubernetes cluster}"
profile_file=${PROFILE_FILE:?set PROFILE_FILE to a reviewed platform profile}
evidence_dir=${EVIDENCE_DIR:-evidence/platform-preflight}
kubectl_bin=${KUBECTL_BIN:-}
[[ -x ${kubectl_bin} ]] || kubectl_bin=$(command -v kubectl)
source "${profile_file}"
mkdir -p "${evidence_dir}"

${kubectl_bin} version -o json >"${evidence_dir}/kubernetes-version.json"
${kubectl_bin} get nodes -o json >"${evidence_dir}/nodes.json"
${kubectl_bin} get pods -A -o json >"${evidence_dir}/pods.json"

node_count=$(jq '.items | length' "${evidence_dir}/nodes.json")
[[ ${node_count} -ge 1 ]] || { echo "no Kubernetes nodes found" >&2; exit 1; }

if [[ ${SINGLE_NODE_CONTROL_PLANE:-false} == true ]]; then
  [[ ${node_count} -eq 1 ]] || {
    echo "single-node profile requires exactly one Kubernetes node" >&2
    exit 1
  }
  blocking_taints=$(jq -r '
    [.items[0].spec.taints // [] | .[] |
      select((.effect == "NoSchedule" or .effect == "NoExecute") and
        (.key == "node-role.kubernetes.io/control-plane" or
         .key == "node-role.kubernetes.io/master"))] | length
  ' "${evidence_dir}/nodes.json")
  if [[ ${blocking_taints} -gt 0 && ${ALLOW_CONTROL_PLANE_SCHEDULING:-false} != true ]]; then
    echo "single control-plane node is tainted; explicitly set ALLOW_CONTROL_PLANE_SCHEDULING=true" >&2
    exit 1
  fi
fi

active_requests=0
if [[ -n ${EXPECTED_NODE_GPU_COUNT:-} ]]; then
  allocatable=$(jq -r --arg gpu "${GPU_RESOURCE_NAME}" \
    '[.items[].status.allocatable[$gpu] // "0" | tonumber] | add' \
    "${evidence_dir}/nodes.json")
  [[ ${allocatable} -eq ${EXPECTED_NODE_GPU_COUNT} ]] || {
    echo "expected ${EXPECTED_NODE_GPU_COUNT} allocatable GPUs, observed ${allocatable}" >&2
    exit 1
  }
  active_requests=$(jq -r --arg gpu "${GPU_RESOURCE_NAME}" '
    [.items[] | select(.status.phase == "Running" or .status.phase == "Pending") |
      .spec.containers[]?.resources.requests[$gpu] // "0" | tonumber] | add
  ' "${evidence_dir}/pods.json")
  [[ ${active_requests} -eq 0 ]] || {
    echo "${active_requests} GPUs are requested by active pods" >&2
    exit 1
  }
fi

cat >"${evidence_dir}/result.env" <<EOF
PLATFORM_PREFLIGHT=PASS
NODE_COUNT=${node_count}
EXPECTED_NODE_GPU_COUNT=${EXPECTED_NODE_GPU_COUNT:-0}
ACTIVE_GPU_REQUESTS=${active_requests}
CONTROL_PLANE_BLOCKING_TAINTS=${blocking_taints:-0}
ALLOW_CONTROL_PLANE_SCHEDULING=${ALLOW_CONTROL_PLANE_SCHEDULING:-false}
EOF
cat "${evidence_dir}/result.env"

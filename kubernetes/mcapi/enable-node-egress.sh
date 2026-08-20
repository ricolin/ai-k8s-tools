#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
: "${KUBECONFIG:?set KUBECONFIG to the target mCAPI cluster}"
profile_file=${PROFILE_FILE:-${root_dir}/kubernetes/profiles/mcapi-emulated.env}
evidence_dir=${EVIDENCE_DIR:-${root_dir}/evidence/mcapi-egress}
source "${profile_file}"

: "${MCAPI_NODE_CIDR:?set MCAPI_NODE_CIDR}"
: "${MCAPI_NODE_GATEWAY:?set MCAPI_NODE_GATEWAY}"
: "${MCAPI_EXTERNAL_INTERFACE:?set MCAPI_EXTERNAL_INTERFACE}"
: "${MCAPI_NODE_SSH_USER:?set MCAPI_NODE_SSH_USER}"
: "${MCAPI_NODE_SSH_KEY:?set MCAPI_NODE_SSH_KEY}"
[[ ${EUID} -eq 0 ]] || { echo "run on the mCAPI management host as root" >&2; exit 1; }
[[ -r ${MCAPI_NODE_SSH_KEY} ]] || { echo "missing SSH key: ${MCAPI_NODE_SSH_KEY}" >&2; exit 1; }
mkdir -p "${evidence_dir}"

rule=(-s "${MCAPI_NODE_CIDR}" -o "${MCAPI_EXTERNAL_INTERFACE}" -m comment
  --comment ai-build-tools-mcapi-egress -j MASQUERADE)
if ! iptables -t nat -C POSTROUTING "${rule[@]}" 2>/dev/null; then
  iptables -t nat -A POSTROUTING "${rule[@]}"
fi
iptables -t nat -S POSTROUTING >"${evidence_dir}/host-postrouting.txt"

mapfile -t nodes < <(kubectl get nodes -o json |
  jq -r '.items[].status.addresses[] | select(.type == "InternalIP") | .address' | sort -u)
[[ ${#nodes[@]} -gt 0 ]] || { echo "no Kubernetes node InternalIP addresses found" >&2; exit 1; }

: >"${evidence_dir}/nodes.txt"
for node in "${nodes[@]}"; do
  printf '%s\n' "${node}" >>"${evidence_dir}/nodes.txt"
  ssh=(ssh -i "${MCAPI_NODE_SSH_KEY}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=10 "${MCAPI_NODE_SSH_USER}@${node}")
  "${ssh[@]}" 'ip route show' >"${evidence_dir}/${node}.routes.before.txt"
  "${ssh[@]}" "sudo ip route replace default via ${MCAPI_NODE_GATEWAY} metric 50"
  "${ssh[@]}" 'ip route show' >"${evidence_dir}/${node}.routes.after.txt"
  "${ssh[@]}" 'curl -fsSI --connect-timeout 15 https://registry-1.docker.io/v2/ >/dev/null || [[ $? -eq 22 ]]'
done

echo "mCAPI node egress is enabled through ${MCAPI_NODE_GATEWAY}; routes are runtime state and must be restored after a node reboot"

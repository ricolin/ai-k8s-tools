#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 REVISION EVIDENCE_DIRECTORY" >&2
  exit 2
fi
revision=$1
evidence_dir=$2
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
profile_file=${PROFILE_FILE:-${root_dir}/kubernetes/profiles/mcapi-emulated.env}
source "${profile_file}"
host_kubeconfig=${HOST_KUBECONFIG:-}
registry_host=${REGISTRY_HOST:-}
registry_namespace=${REGISTRY_NAMESPACE:-ai-release-registry}
registry_route_manifest=${REGISTRY_ROUTE_MANIFEST:-}
: "${host_kubeconfig:?set HOST_KUBECONFIG to the cluster hosting the registry}"
: "${registry_host:?set REGISTRY_HOST to the externally reachable registry name}"
[[ -r ${host_kubeconfig} ]] || { echo "cannot read HOST_KUBECONFIG: ${host_kubeconfig}" >&2; exit 1; }
if [[ -n ${registry_route_manifest} && ! -r ${registry_route_manifest} ]]; then
  echo "cannot read REGISTRY_ROUTE_MANIFEST: ${registry_route_manifest}" >&2
  exit 1
fi
port_forward_pid=
restored=false
mkdir -p "${evidence_dir}"

kube() { KUBECONFIG="${host_kubeconfig}" kubectl "$@"; }

restore_registry() {
  if [[ -n ${port_forward_pid} ]]; then
    kill "${port_forward_pid}" 2>/dev/null || true
    wait "${port_forward_pid}" 2>/dev/null || true
    port_forward_pid=
  fi
  kube -n "${registry_namespace}" set env deployment/registry \
    'REGISTRY_STORAGE_MAINTENANCE_READONLY={"enabled":true}' >/dev/null
  kube -n "${registry_namespace}" rollout status deployment/registry --timeout=120s
  if [[ -s ${evidence_dir}/registry-ingresses.clean.json ]]; then
    kube apply -f "${evidence_dir}/registry-ingresses.clean.json" >/dev/null
  fi
  if [[ -n ${registry_route_manifest} ]]; then
    kube apply -f "${registry_route_manifest}" >/dev/null
  fi
  restored=true
}

on_exit() {
  status=$?
  [[ ${restored} == true ]] || restore_registry || echo "WARNING: registry restoration failed" >&2
  exit "${status}"
}
trap on_exit EXIT INT TERM

readonly_value=$(kube -n "${registry_namespace}" get deployment/registry -o json | jq -r '
  .spec.template.spec.containers[] | select(.name == "registry") |
  .env[]? | select(.name == "REGISTRY_STORAGE_MAINTENANCE_READONLY") | .value')
[[ ${readonly_value} == '{"enabled":true}' ]] || { echo "registry is not read-only" >&2; exit 1; }

kube -n "${registry_namespace}" get ingress -o json >"${evidence_dir}/registry-ingresses.original.json"
jq '{apiVersion:"v1",kind:"List",items:[.items[] | del(
  .metadata.annotations."kubectl.kubernetes.io/last-applied-configuration",
  .metadata.creationTimestamp,.metadata.generation,.metadata.resourceVersion,
  .metadata.uid,.metadata.managedFields,.status)]}' \
  "${evidence_dir}/registry-ingresses.original.json" >"${evidence_dir}/registry-ingresses.clean.json"
kube -n "${registry_namespace}" delete ingress --all
kube -n "${registry_namespace}" set env deployment/registry REGISTRY_STORAGE_MAINTENANCE_READONLY- >/dev/null
kube -n "${registry_namespace}" rollout status deployment/registry --timeout=120s
kube -n "${registry_namespace}" port-forward service/registry 5001:5000 \
  >"${evidence_dir}/registry-port-forward.log" 2>&1 &
port_forward_pid=$!
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:5001/v2/ >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:5001/v2/ >/dev/null

: >"${evidence_dir}/images.env"
for role in workflow runtime; do
  source="localhost/ai-build-tools:${role}-${revision}"
  tag="${role}-${revision}"
  if curl -fsSI "http://127.0.0.1:5001/v2/ai-build-tools/manifests/${tag}" >/dev/null; then
    echo "refusing to overwrite ai-build-tools:${tag}" >&2
    exit 1
  fi
  docker image inspect "${source}" >"${evidence_dir}/${role}.local.json"
  docker tag "${source}" "localhost:5001/ai-build-tools:${tag}"
  docker push "localhost:5001/ai-build-tools:${tag}" | tee "${evidence_dir}/${role}.push.log"
  digest=$(curl -fsSI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "http://127.0.0.1:5001/v2/ai-build-tools/manifests/${tag}" |
    awk 'BEGIN{IGNORECASE=1} /^Docker-Content-Digest:/ {gsub("\r", "", $2); print $2}')
  [[ ${digest} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid image digest" >&2; exit 1; }
  printf 'AI_BUILD_TOOLS_%s_IMAGE=%s/ai-build-tools@%s\n' \
    "${role^^}" "${registry_host}" "${digest}" >>"${evidence_dir}/images.env"
done

restore_registry
trap - EXIT INT TERM

while IFS='=' read -r _ image; do
  digest=${image##*@}
  code=
  for _ in $(seq 1 60); do
    code=$(curl -ksS -o /dev/null -w '%{http_code}' \
      -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
      "https://${registry_host}/v2/ai-build-tools/manifests/${digest}")
    [[ ${code} == 200 ]] && break
    sleep 1
  done
  [[ ${code} == 200 ]] || { echo "manifest probe failed: ${image} HTTP ${code}" >&2; exit 1; }
done <"${evidence_dir}/images.env"
code=$(curl -ksS -o /dev/null -w '%{http_code}' -X POST \
  "https://${registry_host}/v2/ai-build-tools/blobs/uploads/")
[[ ${code} == 405 ]] || { echo "registry write probe returned HTTP ${code}" >&2; exit 1; }
echo "published immutable ai-build-tools images and restored the read-only registry"

#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 LOCAL_IMAGE TARGET_REPOSITORY IMMUTABLE_TAG EVIDENCE_DIRECTORY" >&2
  exit 2
fi
local_image=$1
target_repository=${2#/}
tag=$3
evidence_dir=$4
: "${HOST_KUBECONFIG:?set HOST_KUBECONFIG to the cluster hosting the registry}"
: "${REGISTRY_HOST:?set REGISTRY_HOST to the externally reachable registry name}"
registry_namespace=${REGISTRY_NAMESPACE:-ai-release-registry}
registry_deployment=${REGISTRY_DEPLOYMENT:-registry}
registry_service=${REGISTRY_SERVICE:-registry}
registry_container=${REGISTRY_CONTAINER:-registry}
registry_local_port=${REGISTRY_LOCAL_PORT:-15001}
registry_storage_path=${REGISTRY_STORAGE_PATH:-/var/lib/registry}
: "${REGISTRY_INGRESS:?set REGISTRY_INGRESS to the exact ingress temporarily removed during publication}"
[[ ${registry_local_port} =~ ^[0-9]+$ && ${registry_local_port} -ge 1024 && ${registry_local_port} -le 65535 ]] || {
  echo "REGISTRY_LOCAL_PORT must be an unprivileged TCP port" >&2
  exit 2
}
[[ ${target_repository} =~ ^[a-z0-9][a-z0-9._/-]*$ ]] || { echo "invalid target repository" >&2; exit 2; }
[[ ${tag} =~ ^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$ ]] || { echo "invalid image tag" >&2; exit 2; }
[[ -r ${HOST_KUBECONFIG} ]] || { echo "cannot read HOST_KUBECONFIG" >&2; exit 1; }
mkdir -p "${evidence_dir}"

kube() { KUBECONFIG="${HOST_KUBECONFIG}" kubectl "$@"; }
port_forward_pid=
restored=false
original_readonly_value=

deployment_json=${evidence_dir}/registry-deployment.json
kube -n "${registry_namespace}" get "deployment/${registry_deployment}" -o json >"${deployment_json}"
if [[ ${REGISTRY_PERSISTENCE_CONFIRMED:-false} != true ]]; then
  persistent_storage=$(jq -r --arg container "${registry_container}" --arg path "${registry_storage_path}" '
    .spec.template.spec as $pod |
    [$pod.containers[] | select(.name == $container) | .volumeMounts[]? |
      .mountPath as $mount | select($mount == $path or ($path | startswith($mount + "/"))) |
      .name] as $mounts |
    [$pod.volumes[]? | select(.name as $name | $mounts | index($name)) |
      select(has("persistentVolumeClaim") or has("hostPath"))] | length
  ' "${deployment_json}")
  if [[ ${persistent_storage} -lt 1 ]]; then
    echo "registry storage is not proven persistent at ${registry_storage_path}; refusing a rollout that could discard images" >&2
    echo "set REGISTRY_PERSISTENCE_CONFIRMED=true only after independently proving durable registry storage" >&2
    restored=true
    exit 1
  fi
fi

restore_registry() {
  if [[ -n ${port_forward_pid} ]]; then
    kill "${port_forward_pid}" 2>/dev/null || true
    wait "${port_forward_pid}" 2>/dev/null || true
    port_forward_pid=
  fi
  if [[ -z ${original_readonly_value} ]]; then
    restored=true
    return
  fi
  kube -n "${registry_namespace}" set env "deployment/${registry_deployment}" \
    "REGISTRY_STORAGE_MAINTENANCE_READONLY=${original_readonly_value}" >/dev/null
  kube -n "${registry_namespace}" rollout status \
    "deployment/${registry_deployment}" --timeout=120s
  if [[ -s ${evidence_dir}/registry-ingresses.clean.json ]]; then
    kube apply -f "${evidence_dir}/registry-ingresses.clean.json" >/dev/null
  fi
  restored=true
}

on_exit() {
  status=$?
  [[ ${restored} == true ]] || restore_registry || echo "WARNING: registry restoration failed" >&2
  exit "${status}"
}
trap on_exit EXIT INT TERM

original_readonly_value=$(jq -r --arg container "${registry_container}" '
  .spec.template.spec.containers[] | select(.name == $container) |
  [.env[]? | select(.name == "REGISTRY_STORAGE_MAINTENANCE_READONLY") | .value][0] // empty' \
  "${deployment_json}")
[[ -n ${original_readonly_value} ]] || {
  echo "registry backend does not declare read-only maintenance mode" >&2
  restored=true
  exit 1
}
jq -e '.enabled == true' <<<"${original_readonly_value}" >/dev/null || {
  echo "registry backend is not read-only" >&2
  restored=true
  exit 1
}
external_write_code=$(curl -ksS -o /dev/null -w '%{http_code}' -X POST \
  "https://${REGISTRY_HOST}/v2/${target_repository}/blobs/uploads/")
[[ ${external_write_code} == 405 ]] || {
  echo "external registry must reject writes before publication; HTTP ${external_write_code}" >&2
  exit 1
}
docker image inspect "${local_image}" >"${evidence_dir}/local-image.json"

kube -n "${registry_namespace}" get "ingress/${REGISTRY_INGRESS}" -o json \
  >"${evidence_dir}/registry-ingresses.original.json"
jq '{apiVersion:"v1",kind:"List",items:[. | del(
  .metadata.annotations."kubectl.kubernetes.io/last-applied-configuration",
  .metadata.creationTimestamp,.metadata.generation,.metadata.resourceVersion,
  .metadata.uid,.metadata.managedFields,.status)]}' \
  "${evidence_dir}/registry-ingresses.original.json" >"${evidence_dir}/registry-ingresses.clean.json"
kube -n "${registry_namespace}" delete "ingress/${REGISTRY_INGRESS}"
kube -n "${registry_namespace}" set env "deployment/${registry_deployment}" \
  REGISTRY_STORAGE_MAINTENANCE_READONLY- >/dev/null
kube -n "${registry_namespace}" rollout status \
  "deployment/${registry_deployment}" --timeout=120s
KUBECONFIG="${HOST_KUBECONFIG}" kubectl -n "${registry_namespace}" port-forward \
  --address 127.0.0.1 "service/${registry_service}" "${registry_local_port}:5000" \
  >"${evidence_dir}/registry-port-forward.log" 2>&1 &
port_forward_pid=$!
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${registry_local_port}/v2/" >/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:${registry_local_port}/v2/" >/dev/null

if curl -fsSI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "http://127.0.0.1:${registry_local_port}/v2/${target_repository}/manifests/${tag}" >/dev/null; then
  echo "refusing to overwrite ${target_repository}:${tag}" >&2
  exit 1
fi
push_ref="127.0.0.1:${registry_local_port}/${target_repository}:${tag}"
docker tag "${local_image}" "${push_ref}"
docker push "${push_ref}" 2>&1 | tee "${evidence_dir}/push.log"
digest=$(curl -fsSI -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "http://127.0.0.1:${registry_local_port}/v2/${target_repository}/manifests/${tag}" |
  awk 'BEGIN{IGNORECASE=1} /^Docker-Content-Digest:/ {gsub("\r", "", $2); print $2}')
[[ ${digest} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid image digest" >&2; exit 1; }

restore_registry
trap - EXIT INT TERM
reference="${REGISTRY_HOST}/${target_repository}@${digest}"
printf 'IMAGE_REFERENCE=%s\n' "${reference}" | tee "${evidence_dir}/image.env"
code=$(curl -ksS -o /dev/null -w '%{http_code}' \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "https://${REGISTRY_HOST}/v2/${target_repository}/manifests/${digest}")
[[ ${code} == 200 ]] || { echo "external manifest probe returned HTTP ${code}" >&2; exit 1; }
code=$(curl -ksS -o /dev/null -w '%{http_code}' -X POST \
  "https://${REGISTRY_HOST}/v2/${target_repository}/blobs/uploads/")
[[ ${code} == 405 ]] || { echo "registry write probe returned HTTP ${code}" >&2; exit 1; }
echo "published immutable image and restored the read-only registry"

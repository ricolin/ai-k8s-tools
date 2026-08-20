#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 LOCAL_IMAGE TARGET_IMAGE EVIDENCE_DIRECTORY SSH_TARGET [SSH_OPTION ...]" >&2
  exit 2
fi
local_image=$1
target_image=$2
evidence_dir=$3
ssh_target=$4
shift 4
ssh_options=("$@")
# CUDA layers can be CPU-bound when compressed during a local preload. Keep
# direct streaming as the portable default; callers can opt into gzip when
# bandwidth is the actual bottleneck.
compression=${PRELOAD_COMPRESSION:-none}

[[ ${target_image} =~ ^[a-zA-Z0-9._:/-]+:[a-zA-Z0-9._-]+$ ]] || {
  echo "TARGET_IMAGE must contain an explicit immutable tag" >&2
  exit 2
}
mkdir -p "${evidence_dir}"

docker image inspect "${local_image}" >"${evidence_dir}/source-image.json"
docker tag "${local_image}" "${target_image}"
docker image inspect "${target_image}" >"${evidence_dir}/tagged-image.json"
source_id=$(docker image inspect "${target_image}" --format '{{.Id}}')
[[ ${source_id} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "invalid local image ID" >&2; exit 1; }

case ${compression} in
  gzip)
    docker save "${target_image}" | gzip -1 |
      ssh "${ssh_options[@]}" "${ssh_target}" \
        "gzip -dc | sudo ctr --namespace k8s.io images import --all-platforms=false -"
    ;;
  none)
    docker save "${target_image}" |
      ssh "${ssh_options[@]}" "${ssh_target}" \
        "sudo ctr --namespace k8s.io images import --all-platforms=false -"
    ;;
  *)
    echo "PRELOAD_COMPRESSION must be gzip or none" >&2
    exit 2
    ;;
esac

ssh "${ssh_options[@]}" "${ssh_target}" \
  "sudo crictl inspecti '${target_image}'" >"${evidence_dir}/target-image.json"
target_id=$(jq -r '.status.id // empty' "${evidence_dir}/target-image.json")
[[ ${target_id} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "target runtime did not return an immutable image ID" >&2; exit 1; }

cat >"${evidence_dir}/image.env" <<EOF
NODE_LOCAL_IMAGE=${target_image}
SOURCE_IMAGE_ID=${source_id}
TARGET_IMAGE_ID=${target_id}
EOF
find "${evidence_dir}" -maxdepth 1 -type f ! -name SHA256SUMS -print0 |
  sort -z | xargs -0 sha256sum >"${evidence_dir}/SHA256SUMS"
echo "preloaded ${target_image} as ${target_id} on ${ssh_target}"

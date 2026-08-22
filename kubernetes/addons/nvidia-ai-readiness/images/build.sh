#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 BASE_IMAGE@sha256:DIGEST CUDA_DEVEL_IMAGE@sha256:DIGEST CUDA_RUNTIME_IMAGE@sha256:DIGEST KUBECTL_VERSION KUBECTL_SHA256 OUTPUT_PREFIX" >&2
  exit 2
fi
base_image=$1
cuda_devel_image=$2
cuda_runtime_image=$3
kubectl_version=$4
kubectl_sha256=$5
output_prefix=$6
for image in "${base_image}" "${cuda_devel_image}" "${cuda_runtime_image}"; do
  [[ ${image} =~ @sha256:[a-f0-9]{64}$ ]] || { echo "image must be digest-pinned: ${image}" >&2; exit 2; }
done
[[ ${kubectl_version} =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]
[[ ${kubectl_sha256} =~ ^[a-f0-9]{64}$ ]]

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)
docker build --network host \
  --build-arg BASE_IMAGE="${base_image}" \
  --build-arg KUBECTL_VERSION="${kubectl_version}" \
  --build-arg KUBECTL_SHA256="${kubectl_sha256}" \
  -f "${root_dir}/kubernetes/addons/nvidia-ai-readiness/images/orchestrator/Dockerfile" \
  -t "${output_prefix}-orchestrator" "${root_dir}"
docker build --network host \
  --build-arg CUDA_DEVEL_IMAGE="${cuda_devel_image}" \
  --build-arg CUDA_RUNTIME_IMAGE="${cuda_runtime_image}" \
  -f "${root_dir}/kubernetes/addons/nvidia-ai-readiness/images/cuda-smoke/Dockerfile" \
  -t "${output_prefix}-cuda-smoke" "${root_dir}"
docker image inspect "${output_prefix}-orchestrator" "${output_prefix}-cuda-smoke"

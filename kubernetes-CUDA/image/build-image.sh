#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 || $# -gt 6 ]]; then
  echo "usage: $0 REVISION PYTORCH_CUDA_IMAGE DIFFUSERS_COMMIT OUTPUT_DIR TARGET_REPOSITORY [--push]" >&2
  exit 2
fi
revision=$1
pytorch_cuda_image=$2
diffusers_commit=$3
output_dir=$4
target_repository=${5%/}
mode=${6:-}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

[[ ${revision} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "invalid revision" >&2; exit 2; }
[[ ${pytorch_cuda_image} =~ @sha256:[0-9a-f]{64}$ ]] || { echo "base image must be digest-pinned" >&2; exit 2; }
[[ ${diffusers_commit} =~ ^[0-9a-f]{40}$ ]] || { echo "Diffusers commit must be a full Git SHA" >&2; exit 2; }
[[ -z ${mode} || ${mode} == --push ]] || { echo "optional argument must be --push" >&2; exit 2; }

mkdir -p "${output_dir}"
tag="${target_repository}:image-workflow-${revision}"
cat >"${output_dir}/build-command.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
docker buildx build \\
  --network host \\
  --build-arg PYTORCH_CUDA_IMAGE='${pytorch_cuda_image}' \\
  --build-arg DIFFUSERS_COMMIT='${diffusers_commit}' \\
  --label org.opencontainers.image.revision='${revision}' \\
  --tag '${tag}' \\
  --push \\
  --file '${root_dir}/kubernetes-CUDA/image/Dockerfile' \\
  '${root_dir}'
EOF
chmod 0750 "${output_dir}/build-command.sh"
if [[ ${mode} != --push ]]; then
  echo "Build plan written to ${output_dir}/build-command.sh"
  echo "No image was built or pushed. Add --push to execute it."
  exit 0
fi
"${output_dir}/build-command.sh" 2>&1 | tee "${output_dir}/build.log"
digest=$(docker buildx imagetools inspect "${tag}" --format '{{.Manifest.Digest}}')
[[ ${digest} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "registry digest is invalid" >&2; exit 1; }
printf 'CUDA_IMAGE_WORKFLOW_IMAGE=%s@%s\n' "${target_repository}" "${digest}" >"${output_dir}/image.env"
find "${output_dir}" -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
  | sort -z | xargs -0 sha256sum >"${output_dir}/SHA256SUMS"

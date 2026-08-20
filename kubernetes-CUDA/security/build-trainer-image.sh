#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 REVISION PYTORCH_CUDA_IMAGE OUTPUT_DIR TARGET_REPOSITORY [--push]" >&2
  exit 2
fi

revision=$1
pytorch_cuda_image=$2
output_dir=$3
target_repository=${4%/}
mode=${5:-}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

[[ ${revision} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || {
  echo "invalid revision: ${revision}" >&2
  exit 2
}
[[ ${pytorch_cuda_image} =~ @sha256:[0-9a-f]{64}$ ]] || {
  echo "PYTORCH_CUDA_IMAGE must be digest-pinned" >&2
  exit 2
}
[[ ${target_repository} =~ ^[a-zA-Z0-9._:/-]+$ ]] || {
  echo "invalid target repository" >&2
  exit 2
}
[[ -z ${mode} || ${mode} == --push ]] || {
  echo "the optional fifth argument must be --push" >&2
  exit 2
}

mkdir -p "${output_dir}"
tag="${target_repository}:security-trainer-${revision}"
cat >"${output_dir}/build.env" <<EOF
SECURITY_TRAINER_SOURCE_REVISION=${revision}
SECURITY_TRAINER_BASE_IMAGE=${pytorch_cuda_image}
SECURITY_TRAINER_TAG=${tag}
EOF

cat >"${output_dir}/build-command.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
docker buildx build \\
  --network host \\
  --build-arg PYTORCH_CUDA_IMAGE='${pytorch_cuda_image}' \\
  --label org.opencontainers.image.revision='${revision}' \\
  --tag '${tag}' \\
  --push \\
  --file '${root_dir}/kubernetes-CUDA/security/Dockerfile' \\
  '${root_dir}'
EOF
chmod 0750 "${output_dir}/build-command.sh"

if [[ ${mode} != --push ]]; then
  echo "Build plan written to ${output_dir}/build-command.sh"
  echo "No image was built or pushed. Add --push to execute it."
  exit 0
fi

command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 1
}
"${output_dir}/build-command.sh" 2>&1 | tee "${output_dir}/build.log"
docker buildx imagetools inspect "${tag}" --format '{{json .Manifest}}' \
  >"${output_dir}/registry-manifest.json"
digest=$(docker buildx imagetools inspect "${tag}" --format '{{.Manifest.Digest}}')
[[ ${digest} =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "registry did not return an immutable image digest" >&2
  exit 1
}
printf 'SECURITY_TRAINER_IMAGE=%s@%s\n' "${target_repository}" "${digest}" \
  >"${output_dir}/image.env"
find "${output_dir}" -maxdepth 1 -type f ! -name SHA256SUMS -print0 \
  | sort -z | xargs -0 sha256sum >"${output_dir}/SHA256SUMS"

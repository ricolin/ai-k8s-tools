#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 REVISION PYTORCH_CUDA_IMAGE OUTPUT_DIR TARGET_REPOSITORY [--push|--load]" >&2
  exit 2
fi

revision=$1
pytorch_cuda_image=$2
output_dir=$3
target_repository=${4%/}
mode=${5:-}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

[[ ${revision} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "invalid revision: ${revision}" >&2; exit 2; }
[[ ${pytorch_cuda_image} =~ @sha256:[0-9a-f]{64}$ ]] || { echo "PYTORCH_CUDA_IMAGE must be digest-pinned" >&2; exit 2; }
[[ ${target_repository} =~ ^[a-zA-Z0-9._:/-]+$ ]] || { echo "invalid target repository" >&2; exit 2; }
[[ -z ${mode} || ${mode} == --push || ${mode} == --load ]] || { echo "invalid mode" >&2; exit 2; }

mkdir -p "${output_dir}"
tag="${target_repository}:code-review-trainer-${revision}"
cat >"${output_dir}/build.env" <<EOF
CODE_REVIEW_TRAINER_SOURCE_REVISION=${revision}
CODE_REVIEW_TRAINER_BASE_IMAGE=${pytorch_cuda_image}
CODE_REVIEW_TRAINER_TAG=${tag}
EOF

cat >"${output_dir}/build-command.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
common=(
  --network host
  --build-arg PYTORCH_CUDA_IMAGE='${pytorch_cuda_image}'
  --label org.opencontainers.image.revision='${revision}'
  --tag '${tag}'
  --file '${root_dir}/kubernetes-CUDA/code-review/Dockerfile'
)
docker build "\${common[@]}" '${root_dir}'
if [[ '${mode}' == --push ]]; then docker push '${tag}'; fi
EOF
chmod 0750 "${output_dir}/build-command.sh"

if [[ -z ${mode} ]]; then
  echo "Build plan written to ${output_dir}/build-command.sh"
  exit 0
fi
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
"${output_dir}/build-command.sh" 2>&1 | tee "${output_dir}/build.log"
if [[ ${mode} == --load ]]; then
  docker image inspect "${tag}" >"${output_dir}/local-image.json"
  exit 0
fi
repo_digest=$(docker image inspect "${tag}" --format '{{index .RepoDigests 0}}')
digest=${repo_digest##*@}
[[ ${digest} =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "registry did not return an immutable digest" >&2; exit 1; }
printf 'CODE_REVIEW_TRAINER_IMAGE=%s@%s\n' "${target_repository}" "${digest}" >"${output_dir}/image.env"

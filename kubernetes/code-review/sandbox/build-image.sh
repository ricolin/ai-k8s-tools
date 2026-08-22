#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 REVISION UBUNTU_IMAGE OUTPUT_DIR TARGET_REPOSITORY [--push|--load]" >&2
  exit 2
fi

revision=$1
ubuntu_image=$2
output_dir=$3
target_repository=${4%/}
mode=${5:-}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)

[[ ${revision} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "invalid revision" >&2; exit 2; }
[[ ${ubuntu_image} =~ @sha256:[0-9a-f]{64}$ ]] || { echo "UBUNTU_IMAGE must be digest-pinned" >&2; exit 2; }
[[ -z ${mode} || ${mode} == --push || ${mode} == --load ]] || { echo "invalid mode" >&2; exit 2; }

mkdir -p "${output_dir}"
tag="${target_repository}:code-review-sandbox-${revision}"
cat >"${output_dir}/build-command.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
docker build \
  --network host \
  --build-arg UBUNTU_IMAGE='${ubuntu_image}' \
  --label org.opencontainers.image.revision='${revision}' \
  --tag '${tag}' \
  --file '${root_dir}/kubernetes/code-review/sandbox/Dockerfile' \
  '${root_dir}'
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
printf 'CODE_REVIEW_SANDBOX_IMAGE=%s@%s\n' "${target_repository}" "${digest}" >"${output_dir}/image.env"

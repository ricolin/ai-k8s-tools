#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 EVIDENCE_DIRECTORY [INSTALL_PATH]" >&2
  exit 2
fi

evidence_dir=$1
install_path=${2:-/opt/ai-build-tools-bin/uv}
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source "${root_dir}/kubernetes/versions.env"

archive_name=uv-x86_64-unknown-linux-gnu.tar.gz
archive="${evidence_dir}/${archive_name}"
download="${archive}.new"
temporary=$(mktemp -d /tmp/ai-k8s-tools-uv.XXXXXX)
trap 'rm -rf "${temporary}" "${download}"' EXIT

mkdir -p "${evidence_dir}" "$(dirname "${install_path}")"
curl -fL --retry 10 --retry-delay 2 -o "${download}" \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive_name}"
gzip -t "${download}"
mv -f "${download}" "${archive}"
sha256sum "${archive}" >"${archive}.sha256"
tar -xzf "${archive}" -C "${temporary}"
install -m 0755 \
  "${temporary}/uv-x86_64-unknown-linux-gnu/uv" \
  "${install_path}"

observed_version=$("${install_path}" --version)
[[ ${observed_version} == "uv ${UV_VERSION}" ]] || {
  echo "installed uv version mismatch: ${observed_version}" >&2
  exit 1
}
printf 'UV_BIN=%s\nUV_VERSION=%s\n' "${install_path}" "${UV_VERSION}"

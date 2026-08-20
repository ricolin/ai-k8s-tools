#!/usr/bin/env bash
set -euo pipefail

# Build the workflow and fixture-serving images used by Kubernetes profiles.

if [[ $# -ne 1 ]]; then
  echo "usage: $0 REVISION" >&2
  exit 2
fi
revision=$1
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ ${revision} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || { echo "invalid revision" >&2; exit 2; }

docker build --network host \
  -f "${root_dir}/kubernetes/workflows/Dockerfile" \
  -t "localhost/ai-build-tools:workflow-${revision}" "${root_dir}"
docker build --network host \
  -f "${root_dir}/kubernetes/serving/Dockerfile" \
  -t "localhost/ai-build-tools:runtime-${revision}" "${root_dir}"
docker image inspect \
  "localhost/ai-build-tools:workflow-${revision}" \
  "localhost/ai-build-tools:runtime-${revision}"

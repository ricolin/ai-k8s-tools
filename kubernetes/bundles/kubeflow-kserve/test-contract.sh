#!/usr/bin/env bash
set -euo pipefail
bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root_dir=$(cd "${bundle_dir}/../../.." && pwd)

bash -n "${bundle_dir}"/*.sh
source "${bundle_dir}/source-lock.env"
source "${root_dir}/kubernetes/versions.env"
[[ ${KUBEFLOW_DISTRIBUTION_COMMIT} == f09f3eeaa25cc852665f460497a42b7fc68639ac ]]
[[ ${KUBEFLOW_PIPELINES_VERSION} == 2.16.1 ]]
[[ ${KUBEFLOW_HUB_VERSION} == 0.3.9 ]]
[[ ${KSERVE_VERSION} == 0.18.0 ]]
grep -Fq 'ALLOW_KUBEFLOW_DATA_DELETE' "${bundle_dir}/uninstall.sh"
grep -Fq 'EVIDENCE_DIR=' "${bundle_dir}/install.sh"
echo "PASS: Kubeflow/KServe bundle source and safety contract"

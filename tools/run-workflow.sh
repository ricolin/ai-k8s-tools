#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 RUN_LABEL IMAGES_ENV EVIDENCE_DIRECTORY" >&2
  exit 2
fi
run_label=$1
images_env=$2
evidence_dir=$3
root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
if [[ ${evidence_dir} != /* ]]; then
  evidence_dir="${root_dir}/${evidence_dir}"
fi
profile_file=${PROFILE_FILE:-${root_dir}/kubernetes/profiles/kubernetes-fixture.env}
source "${profile_file}"
source "${images_env}"
: "${KUBECONFIG:?set KUBECONFIG to the target Kubernetes cluster}"
: "${AI_BUILD_TOOLS_WORKFLOW_IMAGE:?missing workflow image}"
: "${AI_BUILD_TOOLS_RUNTIME_IMAGE:?missing runtime image}"
[[ ${run_label} =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { echo "invalid run label" >&2; exit 2; }

kubectl_bin=${KUBECTL_BIN:-/opt/ai-build-tools-bin/kubectl}
uv_bin=${UV_BIN:-/opt/ai-build-tools-bin/uv}
mkdir -p "${evidence_dir}/compiled" "$(dirname "${uv_bin}")"

if [[ ! -x ${uv_bin} ]]; then
  source "${root_dir}/kubernetes/versions.env"
  archive=uv-x86_64-unknown-linux-gnu.tar.gz
  curl -fL --retry 5 -o "${evidence_dir}/${archive}" \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}"
  sha256sum "${evidence_dir}/${archive}" >"${evidence_dir}/${archive}.sha256"
  temporary=$(mktemp -d /tmp/ai-build-tools-uv.XXXXXX)
  tar -xzf "${evidence_dir}/${archive}" -C "${temporary}"
  install -m 0755 "${temporary}/uv-x86_64-unknown-linux-gnu/uv" "${uv_bin}"
fi

cd "${root_dir}/kubernetes/workflows"
"${uv_bin}" sync --python 3.12 --frozen
s3_scheme=http
if [[ ${S3_USE_HTTPS} == 1 ]]; then
  s3_scheme=https
fi
"${uv_bin}" run --frozen python -m ai_build_tools_k8s.pipeline \
  --workflow-image "${AI_BUILD_TOOLS_WORKFLOW_IMAGE}" \
  --node-selector-key "${NODE_SELECTOR_KEY}" \
  --node-selector-value "${NODE_SELECTOR_VALUE}" \
  --s3-endpoint-url "${s3_scheme}://${S3_ENDPOINT}" \
  --output-dir "${evidence_dir}/compiled"

kfp_pid=
registry_pid=
cleanup() {
  for pid in "${kfp_pid}" "${registry_pid}"; do
    if [[ -n ${pid} ]]; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM
"${kubectl_bin}" -n "${KFP_NAMESPACE}" port-forward service/ml-pipeline 8888:8888 \
  >"${evidence_dir}/kfp-port-forward.log" 2>&1 &
kfp_pid=$!
"${kubectl_bin}" -n "${KFP_NAMESPACE}" port-forward service/model-registry-service 8081:8080 \
  >"${evidence_dir}/hub-port-forward.log" 2>&1 &
registry_pid=$!
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8888/apis/v2beta1/healthz >/dev/null \
    && curl -fsS http://127.0.0.1:8081/api/model_registry/v1alpha3/registered_models >/dev/null \
    && break
  sleep 1
done
curl -fsS http://127.0.0.1:8888/apis/v2beta1/healthz >"${evidence_dir}/kfp-health.json"
curl -fsS http://127.0.0.1:8081/api/model_registry/v1alpha3/registered_models \
  >"${evidence_dir}/hub-before.json"

"${uv_bin}" run --frozen python -m ai_build_tools_k8s.orchestrate \
  --train-pipeline "${evidence_dir}/compiled/sdxl-lora-train-and-register.yaml" \
  --deploy-pipeline "${evidence_dir}/compiled/sdxl-lora-deploy-verify-release.yaml" \
  --runtime-image "${AI_BUILD_TOOLS_RUNTIME_IMAGE}" \
  --run-label "${run_label}" \
  --profile "${PROFILE_NAME}" \
  --evidence-class "${EVIDENCE_CLASS}" \
  --evidence-level "${EVIDENCE_LEVEL}" \
  --workload-namespace "${WORKLOAD_NAMESPACE}" \
  --registry-service-host "${MODEL_REGISTRY_HOST}" \
  --registry-service-port "${MODEL_REGISTRY_PORT}" \
  --output "${evidence_dir}/workflow-result.json"

curl -fsS http://127.0.0.1:8081/api/model_registry/v1alpha3/registered_models \
  >"${evidence_dir}/hub-after.json"
"${kubectl_bin}" get nodes -o json >"${evidence_dir}/nodes.json"
"${kubectl_bin}" -n "${KFP_NAMESPACE}" get pod,pvc,workflow -o yaml >"${evidence_dir}/kubeflow-resources.yaml"
"${kubectl_bin}" -n "${WORKLOAD_NAMESPACE}" get inferenceservice,deployment,pod,service -o yaml \
  >"${evidence_dir}/serving-resources.yaml"
jq -e --arg evidence_level "${EVIDENCE_LEVEL}" \
  '.status == "PASS" and .evidence_level == $evidence_level' \
  "${evidence_dir}/workflow-result.json"
echo "PASS: Kubeflow train/release/derive and KServe deployment mechanics completed"

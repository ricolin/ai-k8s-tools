#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source "${root_dir}/kubernetes/versions.env"

: "${KUBECONFIG:?set KUBECONFIG to the target mCAPI cluster}"
profile_file=${PROFILE_FILE:-${root_dir}/kubernetes/profiles/aio-emulated.env}
source "${profile_file}"

source_root=${SOURCE_ROOT:-/opt/ai-build-tools-sources}
tool_root=${TOOL_ROOT:-/opt/ai-build-tools-bin}
evidence_dir=${EVIDENCE_DIR:-${root_dir}/evidence/platform}
mkdir -p "${source_root}" "${tool_root}" "${evidence_dir}"

if [[ ! -x ${tool_root}/kubectl ]]; then
  curl -fL --retry 5 -o "${tool_root}/kubectl" \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
  curl -fL --retry 5 -o "${evidence_dir}/kubectl.sha256.expected" \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
  printf '%s  %s\n' "$(<"${evidence_dir}/kubectl.sha256.expected")" "${tool_root}/kubectl" |
    sha256sum --check
  chmod 0755 "${tool_root}/kubectl"
fi

if [[ ! -x ${tool_root}/kustomize ]]; then
  archive="kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"
  curl -fL --retry 5 -o "${evidence_dir}/${archive}" \
    "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/${archive}"
  sha256sum "${evidence_dir}/${archive}" >"${evidence_dir}/${archive}.sha256"
  tar -xzf "${evidence_dir}/${archive}" -C "${tool_root}" kustomize
  chmod 0755 "${tool_root}/kustomize"
fi

kubectl=${tool_root}/kubectl
kustomize=${tool_root}/kustomize
server_minor=$(${kubectl} version -o json | jq -r '.serverVersion.minor | sub("[^0-9].*$"; "")')
if (( server_minor < 34 )); then
  echo "Kubeflow ${KUBEFLOW_DISTRIBUTION_REF} profile requires the validated Kubernetes 1.34+ baseline" >&2
  exit 1
fi
${kubectl} version -o json >"${evidence_dir}/kubernetes-version.json"
${kustomize} version >"${evidence_dir}/kustomize-version.txt"

source_dir=${source_root}/kubeflow-community-${KUBEFLOW_DISTRIBUTION_COMMIT}
if [[ ! -d ${source_dir}/.git ]]; then
  git clone --filter=blob:none --no-checkout https://github.com/kubeflow/community-distribution.git "${source_dir}"
  git -C "${source_dir}" fetch --depth 1 origin "${KUBEFLOW_DISTRIBUTION_COMMIT}"
  git -C "${source_dir}" checkout --detach "${KUBEFLOW_DISTRIBUTION_COMMIT}"
fi
observed_commit=$(git -C "${source_dir}" rev-parse HEAD)
[[ ${observed_commit} == "${KUBEFLOW_DISTRIBUTION_COMMIT}" ]] || {
  echo "unexpected Kubeflow source commit: ${observed_commit}" >&2
  exit 1
}
printf '%s\n' "${observed_commit}" >"${evidence_dir}/kubeflow-source.commit"

if ! ${kubectl} get storageclass "${STORAGE_CLASS}" >/dev/null 2>&1; then
  ${kubectl} apply -f \
    "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_PROVISIONER_VERSION}/deploy/local-path-storage.yaml"
fi
${kubectl} annotate storageclass "${STORAGE_CLASS}" \
  storageclass.kubernetes.io/is-default-class=true --overwrite
${kubectl} -n local-path-storage rollout status deployment/local-path-provisioner --timeout=300s

cd "${source_dir}"
${kustomize} build common/kubeflow-namespace/base | ${kubectl} apply -f -

${kustomize} build applications/pipeline/upstream/cluster-scoped-resources |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait --for=condition=Established crd/applications.app.k8s.io --timeout=120s
${kustomize} build applications/pipeline/upstream/env/platform-agnostic |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait -n kubeflow --for=condition=Available deployment/mysql --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/seaweedfs --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/ml-pipeline --timeout=900s
${kubectl} wait -n kubeflow --for=condition=Available deployment/ml-pipeline-ui --timeout=900s

${kustomize} build applications/hub/upstream/overlays/db |
  ${kubectl} -n kubeflow apply --server-side --force-conflicts -f -
${kubectl} wait -n kubeflow --for=condition=Available deployment/model-registry-db --timeout=600s
${kubectl} wait -n kubeflow --for=condition=Available deployment/model-registry-deployment --timeout=600s

${kustomize} build common/cert-manager/base | ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait -n cert-manager --for=condition=Ready pod -l app=webhook --timeout=300s
${kustomize} build common/cert-manager/overlays/kubeflow |
  ${kubectl} apply --server-side --force-conflicts -f -
${kubectl} wait -n cert-manager --for=condition=Ready pod \
  -l app.kubernetes.io/instance=cert-manager --timeout=300s

for attempt in 1 2 3; do
  if ${kustomize} build applications/kserve/kserve |
    ${kubectl} apply --server-side --force-conflicts -f -; then
    break
  fi
  [[ ${attempt} -lt 3 ]] || exit 1
  ${kubectl} wait -n kubeflow --for=condition=Ready pod --all --timeout=120s || true
done
${kubectl} wait --for=condition=Established crd/inferenceservices.serving.kserve.io --timeout=120s
${kubectl} wait -n kubeflow --for=condition=Available deployment/kserve-controller-manager --timeout=600s

${kubectl} apply -f "${root_dir}/kubernetes/manifests/aio-integration.yaml"
access_key=$(${kubectl} -n kubeflow get secret mlpipeline-minio-artifact -o jsonpath='{.data.accesskey}' | base64 -d)
secret_key=$(${kubectl} -n kubeflow get secret mlpipeline-minio-artifact -o jsonpath='{.data.secretkey}' | base64 -d)
${kubectl} -n "${WORKLOAD_NAMESPACE}" create secret generic ai-build-tools-s3 \
  --from-literal=AWS_ACCESS_KEY_ID="${access_key}" \
  --from-literal=AWS_SECRET_ACCESS_KEY="${secret_key}" \
  --dry-run=client -o yaml |
  ${kubectl} apply -f -
${kubectl} -n "${WORKLOAD_NAMESPACE}" annotate secret ai-build-tools-s3 \
  serving.kserve.io/s3-endpoint="${S3_ENDPOINT}" \
  serving.kserve.io/s3-usehttps="${S3_USE_HTTPS}" \
  serving.kserve.io/s3-region=us-east-1 \
  serving.kserve.io/s3-useanoncredential=false --overwrite

KUBECTL_BIN="${kubectl}" "${root_dir}/kubernetes/platform/verify.sh" |
  tee "${evidence_dir}/platform-verification.txt"

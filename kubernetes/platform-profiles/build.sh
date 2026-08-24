#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
profile_root=${root}/kubernetes/platform-profiles
source "${profile_root}/source-lock.env"

work=${WORK_DIR:-$(mktemp -d /tmp/ai-platform-profiles.XXXXXX)}
source_dir=${work}/kubeflow-community
bin_dir=${work}/bin
mkdir -p "${bin_dir}"

curl -fsSL -o "${work}/kustomize.tgz" \
  "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"
printf '%s  %s\n' "${KUSTOMIZE_SHA256}" "${work}/kustomize.tgz" | sha256sum -c -
tar -xzf "${work}/kustomize.tgz" -C "${bin_dir}" kustomize

git clone --filter=blob:none --no-checkout \
  "${KUBEFLOW_DISTRIBUTION_REPOSITORY}" "${source_dir}"
git -C "${source_dir}" fetch --depth 1 origin "${KUBEFLOW_DISTRIBUTION_COMMIT}"
git -C "${source_dir}" checkout --detach "${KUBEFLOW_DISTRIBUTION_COMMIT}"
test "$(git -C "${source_dir}" rev-parse HEAD)" = "${KUBEFLOW_DISTRIBUTION_COMMIT}"

curl -fsSL -o "${work}/kueue.tgz" \
  "https://github.com/kubernetes-sigs/kueue/releases/download/v${KUEUE_VERSION}/kueue-${KUEUE_VERSION}.tgz"
printf '%s  %s\n' "${KUEUE_CHART_SHA256}" "${work}/kueue.tgz" | sha256sum -c -
helm pull oci://ghcr.io/kubeflow/charts/kubeflow-trainer \
  --version "${TRAINER_VERSION}" --destination "${work}"
mv "${work}/kubeflow-trainer-${TRAINER_VERSION}.tgz" "${work}/trainer.tgz"
printf '%s  %s\n' "${TRAINER_CHART_SHA256}" "${work}/trainer.tgz" | sha256sum -c -

render_kustomize() {
  local output=$1
  shift
  : >"${output}"
  local target
  for target in "$@"; do
    "${bin_dir}/kustomize" build "${source_dir}/${target}" >>"${output}"
    printf '\n---\n' >>"${output}"
  done
}

render_kustomize "${work}/ai-foundation.raw.yaml" \
  common/kubeflow-namespace/base \
  common/cert-manager/base \
  common/cert-manager/overlays/kubeflow
"${profile_root}/drop-object.py" \
  --input "${work}/ai-foundation.raw.yaml" \
  --output "${work}/ai-foundation.filtered.yaml" \
  --removed-output "${work}/kubeflow-self-signing-issuer.yaml" \
  --kind ClusterIssuer --name kubeflow-self-signing-issuer
mv "${work}/ai-foundation.filtered.yaml" "${work}/ai-foundation.raw.yaml"
render_kustomize "${work}/ai-platform-core.raw.yaml" \
  applications/pipeline/upstream/cluster-scoped-resources \
  applications/pipeline/upstream/env/platform-agnostic \
  applications/hub/upstream/overlays/db
"${profile_root}/drop-object.py" \
  --input "${work}/ai-platform-core.raw.yaml" \
  --output "${work}/ai-platform-core.filtered.yaml" \
  --kind Namespace --name kubeflow
mv "${work}/ai-platform-core.filtered.yaml" "${work}/ai-platform-core.raw.yaml"
printf '\n---\n' >>"${work}/ai-platform-core.raw.yaml"
cat "${work}/kubeflow-self-signing-issuer.yaml" \
  >>"${work}/ai-platform-core.raw.yaml"

helm template ai-scheduling "${work}/kueue.tgz" \
  --namespace kueue-system --include-crds \
  --set enableCertManager=true \
  --set controllerManager.manager.image.repository=registry.k8s.io/kueue/kueue \
  --set controllerManager.manager.image.tag="v${KUEUE_VERSION}" \
  --set-json 'controllerManager.tolerations=[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"},{"key":"node-role.kubernetes.io/master","operator":"Exists","effect":"NoSchedule"}]' \
  >"${work}/ai-scheduling-kueue.raw.yaml"

helm template ai-training "${work}/trainer.tgz" \
  --namespace kubeflow-system --include-crds \
  --set runtimes.torchDistributed.enabled=true \
  --set-json 'manager.tolerations=[{"key":"node-role.kubernetes.io/control-plane","operator":"Exists","effect":"NoSchedule"},{"key":"node-role.kubernetes.io/master","operator":"Exists","effect":"NoSchedule"}]' \
  >"${work}/ai-training-h200.raw.yaml"

render_kustomize "${work}/ai-serving-h200.raw.yaml" applications/kserve/kserve

for chart in ai-foundation ai-platform-core ai-serving-h200; do
  "${profile_root}/add-control-plane-tolerations.py" \
    --input "${work}/${chart}.raw.yaml" \
    --output "${work}/${chart}.tolerations.yaml"
  mv "${work}/${chart}.tolerations.yaml" "${work}/${chart}.raw.yaml"
done

for chart in ai-foundation ai-platform-core ai-scheduling-kueue \
  ai-training-h200 ai-serving-h200; do
  extra=()
  if [[ ${chart} == ai-platform-core ]]; then
    extra+=(--metadata-envoy-concurrency 4)
  fi
  "${profile_root}/pin-manifest-images.py" \
    --lock "${profile_root}/image-lock.tsv" \
    --input "${work}/${chart}.raw.yaml" \
    --output "${work}/${chart}.pinned.yaml" \
    "${extra[@]}"
  "${profile_root}/split-crds.py" \
    --input "${work}/${chart}.pinned.yaml" \
    --resources "${work}/${chart}.resources.yaml" \
    --crds "${work}/${chart}.crds.yaml"
  "${profile_root}/split-resources.py" \
    --input "${work}/${chart}.resources.yaml" \
    --output-dir "${root}/kubernetes/addons/${chart}/files"
  "${profile_root}/split-resources.py" \
    --input "${work}/${chart}.crds.yaml" \
    --output-dir "${root}/kubernetes/addons/${chart}/crds"
done

"${profile_root}/test.sh"
